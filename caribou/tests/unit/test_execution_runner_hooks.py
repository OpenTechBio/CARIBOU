from __future__ import annotations

import io
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from rich.console import Console

from caribou.agents.AgentSystem import Agent, AgentSystem, Command
from caribou.execution import runner


class SequenceLlm:
    def __init__(self, responses: list[str] | None = None, *, error: bool = False):
        self.responses = responses or []
        self.error = error
        self.calls = 0
        self.request_kwargs: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.request_kwargs.append(kwargs)
        if self.error:
            raise RuntimeError("provider unavailable")
        content = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class RecordingSandbox(runner.SandboxManager):
    def __init__(self, *, status: str = "ok") -> None:
        self.status = status
        self.calls: list[str] = []

    def start_container(self) -> bool:
        return True

    def stop_container(self) -> None:
        return None

    def exec_code(self, code: str, timeout: int) -> dict:
        self.calls.append(code)
        return {
            "status": self.status,
            "final_status": self.status,
            "stdout": "executed" if self.status == "ok" else "",
            "stderr": "failure" if self.status != "ok" else "",
        }


def _agents(*, rag: bool = False) -> tuple[AgentSystem, Agent]:
    driver = Agent(
        name="driver",
        prompt="Drive the analysis.",
        commands={
            "delegate_to_coder": Command(
                "delegate_to_coder", "coder", "Delegate implementation"
            )
        },
        code_samples={},
        is_rag_enabled=rag,
    )
    coder = Agent(
        name="coder",
        prompt="Implement the analysis.",
        commands={},
        code_samples={},
    )
    return AgentSystem(
        global_policy="Be accurate", agents={"driver": driver, "coder": coder}
    ), driver


def _run(tmp_path, llm, sandbox, **kwargs):
    agent_system, driver = _agents(rag=kwargs.pop("rag", False))
    return runner.run_agent_session(
        console=Console(file=io.StringIO(), force_terminal=False),
        agent_system=agent_system,
        driver_agent=driver,
        analysis_context="bounded test",
        llm_client=llm,
        sandbox_manager=sandbox,
        history=[{"role": "system", "content": "policy"}],
        is_auto=True,
        max_turns=kwargs.pop("max_turns", 1),
        output_dir=tmp_path,
        **kwargs,
    )


def test_returns_frozen_result_and_emits_code_lifecycle(tmp_path):
    events = []
    sandbox = RecordingSandbox()

    result = _run(
        tmp_path,
        SequenceLlm(["```python\nprint('hello')\n```"]),
        sandbox,
        durable_run_id="run_durable_123",
        event_callback=events.append,
    )

    assert result.schema_version == "caribou.agent_session_result.v1"
    assert result.run_id == "run_durable_123"
    assert result.succeeded is False
    assert result.cancelled is False
    assert result.end_reason == "max_turns_reached"
    assert result.turns_completed == result.final_turn == 1
    assert result.code_blocks_produced == 1
    assert result.code_exec_attempts == 1
    assert result.code_exec_failures == 0
    assert result.current_agent_name == "driver"
    assert result.started_at.endswith("Z")
    assert result.ended_at.endswith("Z")
    with pytest.raises(FrozenInstanceError):
        result.end_reason = "changed"  # type: ignore[misc]

    assert [event["event_type"] for event in events] == [
        "turn_started",
        "assistant_message",
        "code_submitted",
        "code_result",
        "session_end",
    ]
    assert all(event["schema_version"] == "caribou.runner_event.v1" for event in events)
    assert all(event["run_id"] == "run_durable_123" for event in events)
    assert events[2]["payload"]["source"] == "print('hello')"
    assert events[3]["payload"]["success"] is True
    assert events[-1]["payload"]["end_reason"] == "max_turns_reached"
    assert sandbox.calls == ["print('hello')"]


def test_emits_agent_switch_and_rag_attempt_result(tmp_path, monkeypatch):
    class RagClient:
        def query(self, query: str) -> str:
            return f"context for {query}"

    events = []
    monkeypatch.setattr(runner, "get_rag_client", lambda _console: RagClient())
    result = _run(
        tmp_path,
        SequenceLlm(["query_rag_<scanpy>\ndelegate_to_coder"]),
        RecordingSandbox(),
        rag=True,
        event_callback=events.append,
    )

    assert result.current_agent_name == "coder"
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "turn_started",
        "assistant_message",
        "rag_attempt",
        "rag_result",
        "agent_switch",
        "session_end",
    ]
    assert events[3]["payload"] == {
        "query": "scanpy",
        "kind": "knowledge_query",
        "success": True,
        "content": "context for scanpy",
        "error": "",
    }
    assert events[4]["payload"]["from_agent"] == "driver"
    assert events[4]["payload"]["to_agent"] == "coder"


def test_cancellation_is_observed_during_llm_retry_backoff(tmp_path):
    checks = 0
    events = []

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        # Top-of-loop and pre-provider checks pass; the retry wait cancels.
        return checks >= 3

    llm = SequenceLlm(error=True)
    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        durable_run_id="run_cancelled",
        should_cancel=should_cancel,
        event_callback=events.append,
        llm_retry_base_delay=0.0,
        llm_retry_max_delay=0.0,
    )

    assert llm.calls == 1
    assert result.succeeded is False
    assert result.cancelled is True
    assert result.end_reason == "cancelled"
    assert result.turns_completed == 0
    assert result.final_turn == 1
    assert [event["event_type"] for event in events] == [
        "turn_started",
        "session_end",
    ]
    assert events[-1]["payload"]["cancelled"] is True
    assert [event["turn"] for event in events] == [1, 1]


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("llm", "llm_error"),
        ("no_action", "stuck_no_action"),
        ("code", "stuck_code_failures"),
    ],
)
def test_failure_end_reasons_are_not_successful(
    tmp_path, monkeypatch, case: str, expected_reason: str
):
    monkeypatch.setattr(runner, "_LLM_RETRY_BASE_DELAY", 0.0)
    if case == "llm":
        llm = SequenceLlm(error=True)
        sandbox = RecordingSandbox()
    elif case == "no_action":
        llm = SequenceLlm(["I will think about it."])
        sandbox = RecordingSandbox()
    else:
        llm = SequenceLlm(["```python\nraise RuntimeError('bad')\n```"])
        sandbox = RecordingSandbox(status="error")

    result = _run(
        tmp_path,
        llm,
        sandbox,
        max_turns=10,
        llm_retry_base_delay=0.0,
        llm_retry_max_delay=0.0,
    )

    assert result.succeeded is False
    assert result.cancelled is False
    assert result.end_reason == expected_reason


def test_legacy_callers_need_no_new_arguments(tmp_path):
    result = _run(
        tmp_path,
        SequenceLlm(["end_session"]),
        RecordingSandbox(),
    )

    assert result.succeeded is True
    assert result.end_reason == "agent_finished"
    assert result.run_id.startswith("run_")


def test_timeout_after_blocking_provider_returns_is_unsuccessful(tmp_path):
    class SlowLlm(SequenceLlm):
        def _create(self, **kwargs):
            time.sleep(0.02)
            return super()._create(**kwargs)

    events = []
    llm = SlowLlm(["end_session"])
    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        timeout_seconds=0.005,
        event_callback=events.append,
    )

    assert result.succeeded is False
    assert result.cancelled is False
    assert result.end_reason == "timeout"
    assert result.turns_completed == 0
    assert result.final_turn == 1
    assert 0 < llm.request_kwargs[0]["timeout"] <= 0.005
    assert [event["event_type"] for event in events] == [
        "turn_started",
        "session_end",
    ]
    assert [event["turn"] for event in events] == [1, 1]


@pytest.mark.parametrize(
    ("llm", "sandbox", "runner_kwargs", "expected_reason"),
    [
        (
            SequenceLlm(["I will think about it."]),
            RecordingSandbox(),
            {"max_consecutive_no_action": 1},
            "stuck_no_action",
        ),
        (
            SequenceLlm(["```python\nraise RuntimeError('bad')\n```"]),
            RecordingSandbox(status="error"),
            {"max_consecutive_exec_failures": 1},
            "stuck_code_failures",
        ),
    ],
)
def test_configured_failure_thresholds_are_enforced(
    tmp_path, llm, sandbox, runner_kwargs, expected_reason
):
    result = _run(
        tmp_path,
        llm,
        sandbox,
        max_turns=10,
        **runner_kwargs,
    )

    assert result.succeeded is False
    assert result.end_reason == expected_reason
    assert result.turns_completed == 1


def test_configured_retry_attempts_are_enforced(tmp_path):
    llm = SequenceLlm(error=True)
    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        max_turns=10,
        llm_retry_attempts=1,
        llm_retry_base_delay=0.0,
        llm_retry_max_delay=0.0,
    )

    assert llm.calls == 1
    assert result.succeeded is False
    assert result.end_reason == "llm_error"
