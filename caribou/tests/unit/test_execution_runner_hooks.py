from __future__ import annotations

import io
import json
import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from rich.console import Console

from caribou.agents.AgentSystem import Agent, AgentSystem, Command
from caribou.control.records import ProviderCallReceipt
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


class AttemptSequenceLlm:
    """Return or raise the next exact SDK-shaped attempt outcome."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.request_kwargs: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        self.request_kwargs.append(kwargs)
        outcome = self.outcomes[self.calls - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _sdk_response(
    content: str = "end_session",
    *,
    response_id: str | None = None,
    request_id: str | None = None,
    response_model: str | None = None,
    system_fingerprint: str | None = None,
    finish_reason: str | None = None,
    usage: object | None = None,
) -> SimpleNamespace:
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
    )
    response = SimpleNamespace(choices=[choice])
    for key, value in (
        ("id", response_id),
        ("_request_id", request_id),
        ("model", response_model),
        ("system_fingerprint", system_fingerprint),
        ("usage", usage),
    ):
        if value is not None:
            setattr(response, key, value)
    return response


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
    history = kwargs.pop("history", [{"role": "system", "content": "policy"}])
    return runner.run_agent_session(
        console=Console(file=io.StringIO(), force_terminal=False),
        agent_system=agent_system,
        driver_agent=driver,
        analysis_context="bounded test",
        llm_client=llm,
        sandbox_manager=sandbox,
        history=history,
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


def test_llm_attempt_callback_normalizes_only_whitelisted_success_metadata(
    tmp_path,
):
    request_secret = "sk-request-secret"
    response_secret = "sk-response-secret"
    private_url = "https://private.example/v1/chat/completions"
    usage = SimpleNamespace(
        prompt_tokens=101,
        completion_tokens=23,
        total_tokens=124,
        prompt_cache_hit_tokens=11,
        prompt_cache_miss_tokens=90,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=11,
            cache_miss_tokens=90,
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    response = _sdk_response(
        "end_session",
        response_id="resp_exact",
        request_id="req_sdk_exact",
        response_model="returned-model-snapshot",
        system_fingerprint="fp_exact",
        finish_reason="stop",
        usage=usage,
    )
    # These SDK response attributes are deliberately hostile. Receipt
    # normalization must use an allowlist rather than serializing the response.
    response.body = {"content": response_secret, "api_key": request_secret}
    response.headers = {"authorization": f"Bearer {request_secret}"}
    response.url = private_url
    llm = AttemptSequenceLlm([response])
    observations: list[dict[str, object]] = []

    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        durable_run_id="run_" + "1" * 32,
        model_name="requested-model-alias",
        history=[
            {
                "role": "user",
                "content": f"do not persist {request_secret} or {private_url}",
            }
        ],
        llm_attempt_callback=observations.append,
    )

    assert result.succeeded is True
    assert llm.calls == 1
    assert len(observations) == 1
    observation = observations[0]
    assert set(observation) == {
        "turn",
        "agent_name",
        "attempt",
        "maximum_attempts",
        "started_at",
        "ended_at",
        "duration_ms",
        "requested_model",
        "outcome",
        "response_id",
        "request_id",
        "response_model",
        "system_fingerprint",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_miss_tokens",
        "reasoning_tokens",
        "failure_type",
        "http_status_code",
    }
    assert observation | {
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
    } == {
        "turn": 1,
        "agent_name": "driver",
        "attempt": 1,
        "maximum_attempts": 3,
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "requested_model": "requested-model-alias",
        "outcome": "succeeded",
        "response_id": "resp_exact",
        "request_id": "req_sdk_exact",
        "response_model": "returned-model-snapshot",
        "system_fingerprint": "fp_exact",
        "finish_reason": "stop",
        "prompt_tokens": 101,
        "completion_tokens": 23,
        "total_tokens": 124,
        "cached_tokens": 11,
        "cache_miss_tokens": 90,
        "reasoning_tokens": 7,
        "failure_type": None,
        "http_status_code": None,
    }
    assert isinstance(observation["started_at"], str)
    assert str(observation["started_at"]).endswith("Z")
    assert isinstance(observation["ended_at"], str)
    assert str(observation["ended_at"]).endswith("Z")
    assert isinstance(observation["duration_ms"], int)
    assert observation["duration_ms"] >= 0
    serialized = json.dumps(observation, sort_keys=True)
    for forbidden in (
        request_secret,
        response_secret,
        private_url,
        "messages",
        "content",
        "headers",
        "body",
        "api_key",
    ):
        assert forbidden not in serialized


def test_llm_attempt_callback_emits_every_sdk_attempt_and_redacts_failures(tmp_path):
    class UnsafeProviderError(RuntimeError):
        def __init__(self, marker: str) -> None:
            super().__init__(
                f"provider rejected sk-secret-{marker} at "
                f"https://provider.invalid/{marker}"
            )
            self.status_code = 429
            self.body = {"api_key": f"sk-body-{marker}"}
            self.headers = {"authorization": f"Bearer sk-header-{marker}"}

    llm = AttemptSequenceLlm(
        [
            UnsafeProviderError("one"),
            UnsafeProviderError("two"),
            _sdk_response("end_session"),
        ]
    )
    observations: list[dict[str, object]] = []

    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        llm_attempt_callback=observations.append,
        llm_retry_attempts=3,
        llm_retry_base_delay=0.0,
        llm_retry_max_delay=0.0,
    )

    assert result.succeeded is True
    assert llm.calls == 3
    assert [item["attempt"] for item in observations] == [1, 2, 3]
    assert [item["maximum_attempts"] for item in observations] == [3, 3, 3]
    assert [item["outcome"] for item in observations] == [
        "failed",
        "failed",
        "succeeded",
    ]
    assert [item["failure_type"] for item in observations] == [
        "UnsafeProviderError",
        "UnsafeProviderError",
        None,
    ]
    assert [item["http_status_code"] for item in observations] == [429, 429, None]
    serialized = json.dumps(observations, sort_keys=True)
    for forbidden in (
        "sk-secret",
        "sk-body",
        "sk-header",
        "provider.invalid",
        "authorization",
        "headers",
        "body",
    ):
        assert forbidden not in serialized


def test_provider_error_console_output_excludes_raw_exception_details() -> None:
    secret = "sk-console-secret"
    private_url = "https://private.invalid/request"

    class UnsafeProviderError(RuntimeError):
        pass

    llm = AttemptSequenceLlm(
        [
            UnsafeProviderError(f"rejected {secret} at {private_url}"),
            UnsafeProviderError(f"still rejected {secret} at {private_url}"),
        ]
    )
    output = io.StringIO()

    result = runner._call_llm_with_retry(
        console=Console(file=output, force_terminal=False),
        llm_client=llm,
        model_name="requested-model",
        messages=[{"role": "user", "content": "bounded request"}],
        retry_attempts=2,
        retry_base_delay=0.0,
        retry_max_delay=0.0,
    )

    rendered = output.getvalue()
    assert result is None
    assert llm.calls == 2
    assert rendered.count("UnsafeProviderError") == 2
    assert secret not in rendered
    assert private_url not in rendered


@pytest.mark.parametrize(
    "outcome",
    [
        _sdk_response("end_session"),
        RuntimeError("provider failed with sk-secret and https://private.invalid"),
    ],
)
def test_llm_attempt_callback_failure_escapes_without_another_provider_call(
    tmp_path, outcome: object
):
    class ReceiptPersistenceError(RuntimeError):
        pass

    llm = AttemptSequenceLlm([outcome, _sdk_response("must not be called")])
    callback_calls = 0

    def fail_receipt_persistence(_: dict[str, object]) -> None:
        nonlocal callback_calls
        callback_calls += 1
        raise ReceiptPersistenceError("durable receipt write failed")

    with pytest.raises(ReceiptPersistenceError, match="durable receipt write failed"):
        _run(
            tmp_path,
            llm,
            RecordingSandbox(),
            llm_attempt_callback=fail_receipt_persistence,
            llm_retry_attempts=3,
            llm_retry_base_delay=0.0,
            llm_retry_max_delay=0.0,
        )

    assert callback_calls == 1
    assert llm.calls == 1


def test_cancellation_after_sdk_response_still_emits_attempt_observation(tmp_path):
    checks = 0
    observations: list[dict[str, object]] = []

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        # Session top and pre-provider checks pass; post-response check cancels.
        return checks >= 3

    llm = AttemptSequenceLlm([_sdk_response("end_session")])
    result = _run(
        tmp_path,
        llm,
        RecordingSandbox(),
        durable_run_id="run_" + "2" * 32,
        should_cancel=should_cancel,
        llm_attempt_callback=observations.append,
    )

    assert llm.calls == 1
    assert result.cancelled is True
    assert result.end_reason == "cancelled"
    assert len(observations) == 1
    assert observations[0]["outcome"] == "succeeded"


def test_sparse_sdk_response_normalizes_all_missing_metadata_to_null(tmp_path):
    observations: list[dict[str, object]] = []
    result = _run(
        tmp_path,
        AttemptSequenceLlm([_sdk_response("end_session")]),
        RecordingSandbox(),
        llm_attempt_callback=observations.append,
    )

    assert result.succeeded is True
    assert {
        key: observations[0][key]
        for key in (
            "response_id",
            "request_id",
            "response_model",
            "system_fingerprint",
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "cache_miss_tokens",
            "reasoning_tokens",
            "failure_type",
            "http_status_code",
        )
    } == {
        "response_id": None,
        "request_id": None,
        "response_model": None,
        "system_fingerprint": None,
        "finish_reason": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
        "cache_miss_tokens": None,
        "reasoning_tokens": None,
        "failure_type": None,
        "http_status_code": None,
    }


def _valid_provider_call_receipt() -> dict[str, object]:
    run_id = "run_" + "3" * 32
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "caribou.provider_call_receipt.v1",
        "call_id": f"{run_id}:turn:2:attempt:1",
        "run_id": run_id,
        "turn": 2,
        "agent_name": "driver",
        "attempt": 1,
        "maximum_attempts": 1,
        "provider": "deepseek",
        "requested_model": "requested-model",
        "outcome": "succeeded",
        "started_at": now,
        "ended_at": now,
        "duration_ms": 0,
        "response_id": "resp_exact",
        "request_id": "req_exact",
        "response_model": "returned-model",
        "system_fingerprint": "fp_exact",
        "finish_reason": "stop",
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "cached_tokens": 2,
            "cache_miss_tokens": 8,
            "reasoning_tokens": 1,
        },
        "failure_type": None,
        "http_status_code": None,
        "cost_usd": None,
        "cost_basis": "unavailable",
        "sdk_retries": 0,
    }


def test_provider_call_receipt_is_strict_versioned_and_consistent() -> None:
    receipt = ProviderCallReceipt.model_validate(_valid_provider_call_receipt())
    assert receipt.call_id == f"{receipt.run_id}:turn:2:attempt:1"
    assert receipt.response_model == "returned-model"
    assert receipt.requested_model == "requested-model"
    assert receipt.usage.cached_tokens == 2
    assert receipt.cost_usd is None
    assert receipt.cost_basis == "unavailable"
    assert receipt.sdk_retries == 0

    invalid_updates = (
        {"unexpected": "field"},
        {"call_id": f"{receipt.run_id}:turn:99:attempt:1"},
        {"duration_ms": 0.5},
        {"sdk_retries": 1},
        {"cost_usd": 0.0},
        {
            "usage": {
                **dict(_valid_provider_call_receipt()["usage"]),
                "total_tokens": 14.0,
            }
        },
        {"outcome": "failed", "failure_type": None},
    )
    for updates in invalid_updates:
        candidate = {**_valid_provider_call_receipt(), **updates}
        with pytest.raises(ValidationError):
            ProviderCallReceipt.model_validate(candidate)
