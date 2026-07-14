from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from caribou.agents.AgentSystem import Agent, AgentSystem
from caribou.control.agent_workload import (
    _CancellationAwareSandbox,
    _provider_client,
    _verify_blueprint_dependencies,
    _verify_code_identity,
)
from caribou.control.api import ControlError
from caribou.control.specs import CARIBOU_AGENT_ADAPTER, _validate_agent_adapter
from caribou.domain.models import ToolSpec
from caribou.domain.serialization import sha256_bytes

from .test_domain_models import make_spec


class WaitingBackend:
    def __init__(self) -> None:
        self.cancel_seen = False

    def exec_code(
        self,
        code: str,
        *,
        timeout: int,
        cancel_event: threading.Event,
    ) -> dict[str, object]:
        assert code == "wait"
        assert timeout == 10
        self.cancel_seen = cancel_event.wait(1)
        return {
            "status": "cancelled" if self.cancel_seen else "timeout",
            "stdout": "",
            "stderr": "",
        }


def test_real_sandbox_adapter_propagates_durable_cancellation() -> None:
    cancel = threading.Event()
    backend = WaitingBackend()
    sandbox = _CancellationAwareSandbox(backend, cancel.is_set)
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        result = sandbox.exec_code("wait", timeout=10)
    finally:
        timer.join()

    assert result["status"] == "cancelled"
    assert backend.cancel_seen is True


def test_real_blueprint_dependencies_are_exactly_bound() -> None:
    content = "print('bound')\n"
    agent = Agent(
        name="analyst",
        prompt="Analyze",
        commands={},
        code_samples={"bound.py": content},
        is_rag_enabled=False,
    )
    system = AgentSystem(global_policy="policy", agents={"analyst": agent})
    run = SimpleNamespace(
        resolved_blueprint=SimpleNamespace(
            code_sample_hashes={"bound.py": sha256_bytes(content.encode("utf-8"))},
            rag_corpus=None,
        )
    )

    _verify_blueprint_dependencies(system, run)
    run.resolved_blueprint.code_sample_hashes = {}
    with pytest.raises(ControlError) as exc_info:
        _verify_blueprint_dependencies(system, run)
    assert exc_info.value.code == "CODE_SAMPLE_HASH_MISMATCH"


def test_real_code_identity_does_not_trust_environment_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = "a" * 40
    actual = "b" * 40
    monkeypatch.setenv("CARIBOU_CODE_COMMIT", expected)

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if "--show-toplevel" in command:
            return SimpleNamespace(returncode=0, stdout=str(tmp_path) + "\n")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=actual + "\n")
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(command)

    monkeypatch.setattr(
        "caribou.control.agent_workload.subprocess.run",
        fake_run,
    )

    with pytest.raises(ControlError) as exc_info:
        _verify_code_identity(expected, CARIBOU_AGENT_ADAPTER)
    assert exc_info.value.code == "CODE_COMMIT_MISMATCH"
    assert exc_info.value.details["actual"] == actual


@pytest.mark.parametrize(
    ("runtime_version", "tools", "expected_code"),
    [
        ("apptainer 1.3", [], "AGENT_RUNTIME_VERSION_UNSUPPORTED"),
        (
            None,
            [ToolSpec(name="scanpy", version="1.11")],
            "AGENT_TOOLS_UNSUPPORTED",
        ),
    ],
)
def test_real_adapter_rejects_unbound_runtime_and_tool_fields(
    runtime_version: str | None,
    tools: list[ToolSpec],
    expected_code: str,
) -> None:
    spec = make_spec()
    condition = spec.conditions[0]
    condition = condition.model_copy(
        update={
            "model": condition.model.model_copy(
                update={
                    "provider": "openai",
                    "model": "frozen-model-id",
                    "context_length": None,
                }
            ),
            "blueprint": condition.blueprint.model_copy(update={"tools": tools}),
        }
    )
    container = spec.execution.container.model_copy(
        update={"runtime_version": runtime_version}
    )
    spec = spec.model_copy(
        update={
            "conditions": [condition],
            "execution": spec.execution.model_copy(update={"container": container}),
        }
    )

    with pytest.raises(ControlError) as exc_info:
        _validate_agent_adapter(spec, 0, CARIBOU_AGENT_ADAPTER)
    assert exc_info.value.code == expected_code


def test_real_adapter_rejects_unclassified_retry_policy() -> None:
    spec = make_spec()
    condition = spec.conditions[0]
    condition = condition.model_copy(
        update={
            "model": condition.model.model_copy(
                update={
                    "provider": "openai",
                    "model": "frozen-model-id",
                    "context_length": None,
                }
            )
        }
    )
    counters = {
        name: counter.model_copy(update={"limit": None})
        for name, counter in spec.budget
    }
    spec = spec.model_copy(
        update={
            "conditions": [condition],
            "budget": spec.budget.model_copy(update=counters),
            "stop_rules": spec.stop_rules.model_copy(
                update={
                    "retry": spec.stop_rules.retry.model_copy(
                        update={"maximum_attempts": 2}
                    )
                }
            ),
        }
    )

    with pytest.raises(ControlError) as exc_info:
        _validate_agent_adapter(spec, 0, CARIBOU_AGENT_ADAPTER)
    assert exc_info.value.code == "AGENT_RETRY_POLICY_UNSUPPORTED"


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_external_provider_disables_sdk_retries(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "test-key")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: calls.append(kwargs) or object())

    _provider_client(provider, {})

    assert calls[0]["max_retries"] == 0
