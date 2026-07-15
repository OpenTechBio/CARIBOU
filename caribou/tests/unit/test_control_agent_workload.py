from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from caribou.agents.AgentSystem import Agent, AgentSystem, Command
from caribou.control.agent_workload import (
    _CancellationAwareSandbox,
    _event_recorder,
    _provider_client,
    _provider_receipt_recorder,
    _verify_blueprint_dependencies,
    _verify_code_identity,
    execute_agent_workload,
)
from caribou.control.api import ControlError
from caribou.control.specs import (
    AGENT_PATH_SMOKE_ADAPTER,
    CARIBOU_AGENT_ADAPTER,
    _validate_agent_adapter,
)
from caribou.control.store import ExperimentStore
from caribou.domain.enums import (
    EventType,
    RunState,
    SandboxKind,
    TopologyKind,
)
from caribou.domain.models import (
    ContentReference,
    ExperimentSpec,
    ModelSpec,
    ToolSpec,
)
from caribou.domain.serialization import file_hash, sha256_bytes
from caribou.execution.runner import AgentSessionResult

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


def test_real_blueprint_binds_rag_enabled_agents_to_frozen_corpus(
    tmp_path: Path,
) -> None:
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        json.dumps(
            {
                "schema_version": "caribou.rag_corpus.v1",
                "entries": [
                    {
                        "title": "annotation markers",
                        "keywords": ["cell typing"],
                        "content": "Use frozen marker evidence.",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    agent = Agent(
        name="analyst",
        prompt="Analyze",
        commands={},
        code_samples={},
        is_rag_enabled=True,
    )
    system = AgentSystem(global_policy="policy", agents={"analyst": agent})
    run = SimpleNamespace(
        resolved_blueprint=SimpleNamespace(
            code_sample_hashes={},
            rag_corpus=_local_reference(corpus_path, "application/json"),
        )
    )

    assert _verify_blueprint_dependencies(system, run) == corpus_path.resolve()

    run.resolved_blueprint.rag_corpus = None
    with pytest.raises(ControlError) as exc_info:
        _verify_blueprint_dependencies(system, run)
    assert exc_info.value.code == "RAG_NOT_BOUND"


def test_real_blueprint_rejects_unused_rag_corpus(tmp_path: Path) -> None:
    corpus_path = tmp_path / "unused-corpus.json"
    corpus_path.write_text("{}\n", encoding="utf-8")
    agent = Agent(
        name="analyst",
        prompt="Analyze",
        commands={},
        code_samples={},
        is_rag_enabled=False,
    )
    system = AgentSystem(global_policy="policy", agents={"analyst": agent})
    run = SimpleNamespace(
        resolved_blueprint=SimpleNamespace(
            code_sample_hashes={},
            rag_corpus=_local_reference(corpus_path, "application/json"),
        )
    )

    with pytest.raises(ControlError) as exc_info:
        _verify_blueprint_dependencies(system, run)
    assert exc_info.value.code == "RAG_CORPUS_UNUSED"


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


def _real_adapter_spec_with_retry(
    *, maximum_attempts: int, retryable_categories: list[str]
) -> ExperimentSpec:
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
                        update={
                            "maximum_attempts": maximum_attempts,
                            "retryable_categories": retryable_categories,
                            "base_delay_seconds": 2.0,
                            "maximum_delay_seconds": 10.0,
                        }
                    )
                }
            ),
        }
    )
    return ExperimentSpec.model_validate_json(spec.model_dump_json())


def test_real_adapter_accepts_bounded_provider_retry_policy() -> None:
    spec = _real_adapter_spec_with_retry(
        maximum_attempts=3,
        retryable_categories=["provider", "timeout"],
    )

    _validate_agent_adapter(spec, 0, CARIBOU_AGENT_ADAPTER)


def test_real_adapter_accepts_frozen_max_output_tokens() -> None:
    spec = _real_adapter_spec_with_retry(
        maximum_attempts=3,
        retryable_categories=["provider", "timeout"],
    )
    condition = spec.conditions[0].model_copy(
        update={
            "model": spec.conditions[0].model.model_copy(
                update={"parameters": {"max_output_tokens": 8192}}
            )
        }
    )
    spec = spec.model_copy(update={"conditions": [condition]})

    _validate_agent_adapter(spec, 0, CARIBOU_AGENT_ADAPTER)


@pytest.mark.parametrize(
    ("parameters", "expected_code"),
    [
        ({"max_output_tokens": 0}, "AGENT_MODEL_PARAMETER_INVALID"),
        ({"max_output_tokens": True}, "AGENT_MODEL_PARAMETER_INVALID"),
        ({"temperature": 0}, "AGENT_MODEL_PARAMETERS_UNSUPPORTED"),
    ],
)
def test_real_adapter_rejects_invalid_or_unbound_model_parameters(
    parameters: dict[str, object], expected_code: str
) -> None:
    spec = _real_adapter_spec_with_retry(
        maximum_attempts=3,
        retryable_categories=["provider", "timeout"],
    )
    condition = spec.conditions[0].model_copy(
        update={
            "model": spec.conditions[0].model.model_copy(
                update={"parameters": parameters}
            )
        }
    )
    spec = spec.model_copy(update={"conditions": [condition]})

    with pytest.raises(ControlError) as exc_info:
        _validate_agent_adapter(spec, 0, CARIBOU_AGENT_ADAPTER)
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    "retryable_categories",
    [[], ["timeout"], ["provider", "scheduler"]],
)
def test_real_adapter_rejects_unenforceable_retry_policy(
    retryable_categories: list[str],
) -> None:
    spec = _real_adapter_spec_with_retry(
        maximum_attempts=2,
        retryable_categories=retryable_categories,
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
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kwargs: calls.append(kwargs) or object()
    )

    _provider_client(provider, {})

    assert calls[0]["max_retries"] == 0


def _local_reference(path: Path, media_type: str) -> ContentReference:
    return ContentReference(
        uri=path.resolve().as_uri(),
        content_hash=file_hash(path),
        size_bytes=path.stat().st_size,
        media_type=media_type,
    )


def _workload_spec(
    tmp_path: Path,
    adapter: str,
    *,
    max_output_tokens: int | None = None,
) -> ExperimentSpec:
    blueprint_path = tmp_path / f"{adapter}-blueprint.json"
    blueprint_path.write_text("{}\n", encoding="utf-8")
    prompt_path = tmp_path / f"{adapter}-prompt.txt"
    prompt_path.write_text("Run a bounded provider receipt test.\n", encoding="utf-8")
    input_path = tmp_path / f"{adapter}-input.h5ad"
    input_path.write_bytes(b"provider-receipt-input\n")
    image_path = tmp_path / f"{adapter}-image.fixture"
    image_path.write_bytes(b"provider-receipt-container\n")

    base = make_spec()
    is_smoke = adapter == AGENT_PATH_SMOKE_ADAPTER
    blueprint = base.conditions[0].blueprint.model_copy(
        update={
            "source": _local_reference(blueprint_path, "application/json"),
            "topology": (
                TopologyKind.multi_agent if is_smoke else TopologyKind.single_agent
            ),
            "driver_agent": "analyst",
            "code_sample_hashes": {},
            "rag_corpus": None,
            "tools": [],
        }
    )
    model = (
        ModelSpec(
            provider="scripted",
            model="caribou-agent-path-smoke@v1",
            context_length=8192,
        )
        if is_smoke
        else ModelSpec(
            provider="deepseek",
            model="frozen-request-model",
            parameters=(
                {"max_output_tokens": max_output_tokens}
                if max_output_tokens is not None
                else {}
            ),
        )
    )
    parameters: dict[str, object] = {"caribou.execution_adapter": adapter}
    if is_smoke:
        parameters["caribou.agent_smoke_delay_seconds"] = 0.0
    condition = base.conditions[0].model_copy(
        update={
            "blueprint": blueprint,
            "model": model,
            "prompt": _local_reference(prompt_path, "text/plain"),
            "parameters": parameters,
        }
    )
    container = base.execution.container.model_copy(
        update={
            "sandbox": SandboxKind.offline if is_smoke else SandboxKind.apptainer,
            "image": _local_reference(image_path, "application/octet-stream"),
            "runtime_version": None,
            "gpu_enabled": False,
            "network_enabled": False,
            "force_refresh": False,
            "bind_mounts": {},
        }
    )
    execution = base.execution.model_copy(
        update={"container": container, "output_root": f"runs/{adapter}"}
    )
    unlimited_budget = base.budget.model_copy(
        update={
            name: counter.model_copy(update={"limit": None})
            for name, counter in base.budget
        }
    )
    stop_rules = base.stop_rules.model_copy(
        update={"maximum_turns": 3 if is_smoke else 1}
    )
    candidate = base.model_copy(
        update={
            "inputs": [_local_reference(input_path, "application/x-hdf5")],
            "conditions": [condition],
            "execution": execution,
            "budget": unlimited_budget,
            "stop_rules": stop_rules,
            "repetitions": 1,
        }
    )
    return ExperimentSpec.model_validate_json(candidate.model_dump_json())


def _active_store(
    tmp_path: Path,
    adapter: str,
    *,
    max_output_tokens: int | None = None,
) -> tuple[ExperimentStore, str, ExperimentSpec]:
    spec = _workload_spec(
        tmp_path,
        adapter,
        max_output_tokens=max_output_tokens,
    )
    store = ExperimentStore(tmp_path / f"{adapter}-store")
    submission = store.submit(spec, f"{adapter}-provider-receipt-test")
    run_id = submission.runs[0].run_id
    store.transition_run(
        run_id,
        RunState.starting,
        reason="unit-test worker started",
        actor="test-worker",
    )
    return store, run_id, spec


def _attempt_observation(
    *,
    requested_model: str = "frozen-request-model",
    response_model: str | None = "returned-model-snapshot",
) -> dict[str, object]:
    return {
        "turn": 2,
        "agent_name": "analyst",
        "attempt": 1,
        "maximum_attempts": 1,
        "started_at": "2026-07-14T12:00:00Z",
        "ended_at": "2026-07-14T12:00:01Z",
        "duration_ms": 1000,
        "requested_model": requested_model,
        "outcome": "succeeded",
        "response_id": "resp_exact",
        "request_id": "req_sdk_exact",
        "response_model": response_model,
        "system_fingerprint": "fp_exact",
        "finish_reason": "stop",
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "total_tokens": 125,
        "cached_tokens": 10,
        "cache_miss_tokens": 90,
        "reasoning_tokens": 4,
        "failure_type": None,
        "http_status_code": None,
    }


def test_provider_receipt_recorder_persists_deterministic_artifact_and_event(
    tmp_path: Path,
) -> None:
    store, run_id, _ = _active_store(tmp_path, CARIBOU_AGENT_ADAPTER)
    store.transition_run(
        run_id,
        RunState.running,
        reason="provider receipt unit test initialized",
        actor="test-worker",
    )

    _provider_receipt_recorder(store, run_id)(_attempt_observation())

    manifest = store.artifact_manifest(run_id)
    assert len(manifest.artifacts) == 1
    artifact = manifest.artifacts[0]
    assert artifact.filename == "provider-call-turn-2-attempt-1.json"
    assert artifact.role == "provider_call_receipt"
    assert artifact.producer == "provider-client"
    assert artifact.schema_type == "caribou.provider_call_receipt"
    assert artifact.schema_version_name == "v1"
    receipt = json.loads(store.artifact_path(artifact).read_text(encoding="utf-8"))
    assert receipt["call_id"] == f"{run_id}:turn:2:attempt:1"
    assert receipt["provider"] == "deepseek"
    assert receipt["requested_model"] == "frozen-request-model"
    assert receipt["response_model"] == "returned-model-snapshot"
    assert receipt["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 25,
        "total_tokens": 125,
        "cached_tokens": 10,
        "cache_miss_tokens": 90,
        "reasoning_tokens": 4,
    }
    assert receipt["cost_usd"] is None
    assert receipt["cost_basis"] == "unavailable"
    assert receipt["sdk_retries"] == 0

    artifact_events = [
        event
        for event in store.events(run_id)
        if event.event_type == EventType.artifact_created
    ]
    assert len(artifact_events) == 1
    event = artifact_events[0]
    assert artifact.producer_event_id == event.event_id
    assert event.payload.artifact_id == artifact.artifact_id
    assert event.turn == 2
    assert store.run(run_id).current_agent == "analyst"


def test_provider_receipt_replay_is_idempotent_and_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    store, run_id, _ = _active_store(tmp_path, CARIBOU_AGENT_ADAPTER)
    store.transition_run(
        run_id,
        RunState.running,
        reason="provider receipt unit test initialized",
        actor="test-worker",
    )
    recorder = _provider_receipt_recorder(store, run_id)
    observation = _attempt_observation()

    recorder(observation)
    recorder(dict(observation))

    assert len(store.artifact_manifest(run_id).artifacts) == 1
    artifact_events = [
        event
        for event in store.events(run_id)
        if event.event_type == EventType.artifact_created
    ]
    assert len(artifact_events) == 1

    with pytest.raises(ControlError) as exc_info:
        recorder({**observation, "response_id": "different-response"})
    assert exc_info.value.code == "IDEMPOTENT_ARTIFACT_CONFLICT"
    assert len(store.artifact_manifest(run_id).artifacts) == 1


def test_provider_receipt_repairs_manifest_journal_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import caribou.control.store as store_module

    store, run_id, _ = _active_store(tmp_path, CARIBOU_AGENT_ADAPTER)
    store.transition_run(
        run_id,
        RunState.running,
        reason="provider receipt unit test initialized",
        actor="test-worker",
    )
    real_commit = store_module.commit_run_event
    calls = 0

    def interrupt_once(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected manifest-journal interruption")
        return real_commit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "commit_run_event", interrupt_once)

    _provider_receipt_recorder(store, run_id)(_attempt_observation())

    manifest = store.artifact_manifest(run_id)
    assert calls == 2
    assert len(manifest.artifacts) == 1
    artifact = manifest.artifacts[0]
    assert store.run(run_id).artifact_ids == [artifact.artifact_id]
    events = [
        event
        for event in store.events(run_id)
        if event.event_type == EventType.artifact_created
    ]
    assert len(events) == 1
    assert events[0].event_id == artifact.producer_event_id


def test_provider_receipt_recorder_rejects_frozen_request_model_drift(
    tmp_path: Path,
) -> None:
    store, run_id, _ = _active_store(tmp_path, CARIBOU_AGENT_ADAPTER)
    store.transition_run(
        run_id,
        RunState.running,
        reason="provider receipt unit test initialized",
        actor="test-worker",
    )

    with pytest.raises(RuntimeError, match="differs from frozen run"):
        _provider_receipt_recorder(store, run_id)(
            _attempt_observation(requested_model="unfrozen-request-model")
        )

    assert store.artifact_manifest(run_id).artifacts == ()


def test_ignored_code_blocks_runner_event_is_recorded_durably(
    tmp_path: Path,
) -> None:
    store, run_id, _ = _active_store(tmp_path, CARIBOU_AGENT_ADAPTER)
    store.transition_run(
        run_id,
        RunState.running,
        reason="ignored-code-block unit test initialized",
        actor="test-worker",
    )

    _event_recorder(store, run_id)(
        {
            "schema_version": "caribou.runner_event.v1",
            "event_type": "code_blocks_ignored",
            "occurred_at": "2026-07-15T12:00:00Z",
            "run_id": run_id,
            "turn": 3,
            "agent_name": "analyst",
            "payload": {
                "total_blocks_produced": 147,
                "executed_blocks": 1,
                "ignored_blocks": 146,
                "reason": "maximum one code block per provider turn",
            },
        }
    )

    event = store.events(run_id)[-1]
    assert event.event_type == EventType.heartbeat
    assert event.stage == "code_block_limit"
    assert event.turn == 3
    assert store.run(run_id).current_agent == "analyst"
    assert event.payload.message == (
        "ignored 146 additional provider code block(s); executed only the first "
        "complete block"
    )


class _NonExecutingBackend:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def set_data(self, _: list[tuple[Path, str]], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)

    def start_container(self) -> bool:
        self.started = True
        return True

    def stop_container(self) -> None:
        self.stopped = True

    def exec_code(
        self,
        code: str,
        *,
        timeout: int,
        cancel_event: threading.Event,
    ) -> dict[str, object]:
        raise AssertionError((code, timeout, cancel_event))


def _successful_session(run_id: str) -> AgentSessionResult:
    return AgentSessionResult(
        schema_version="caribou.agent_session_result.v1",
        run_id=run_id,
        succeeded=True,
        cancelled=False,
        end_reason="agent_finished",
        turns_completed=2,
        code_blocks_produced=0,
        code_exec_attempts=0,
        code_exec_failures=0,
        correction_count=0,
        current_agent_name="analyst",
        final_turn=2,
        started_at="2026-07-14T12:00:00Z",
        ended_at="2026-07-14T12:00:01Z",
        duration_seconds=1.0,
    )


@pytest.mark.parametrize(
    ("adapter", "expected_provider_receipts"),
    [
        (CARIBOU_AGENT_ADAPTER, 1),
        (AGENT_PATH_SMOKE_ADAPTER, 0),
    ],
)
def test_workload_wires_receipts_only_for_actual_provider_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter: str,
    expected_provider_receipts: int,
) -> None:
    store, run_id, _ = _active_store(tmp_path, adapter)
    command = Command(
        name="delegate_to_general",
        target_agent="analyst",
        description="scripted smoke delegation",
    )
    system = AgentSystem(
        global_policy="policy",
        agents={
            "analyst": Agent(
                name="analyst",
                prompt="analyze",
                commands={"delegate_to_general": command},
                code_samples={},
            )
        },
    )
    monkeypatch.setattr(
        AgentSystem,
        "load_from_json",
        classmethod(lambda cls, path: system),
    )
    monkeypatch.setattr(
        "caribou.control.agent_workload._verify_code_identity",
        lambda expected, selected_adapter: None,
    )
    backend = _NonExecutingBackend()
    monkeypatch.setattr(
        "caribou.control.agent_workload._real_sandbox",
        lambda *args, **kwargs: backend,
    )
    monkeypatch.setattr(
        "caribou.control.agent_workload._provider_client",
        lambda *args, **kwargs: object(),
    )
    observed_callbacks: list[Callable[[dict[str, object]], None] | None] = []

    def fake_run_agent_session(**kwargs: object) -> AgentSessionResult:
        callback = kwargs["llm_attempt_callback"]
        assert callback is None or callable(callback)
        observed_callbacks.append(callback)  # type: ignore[arg-type]
        if callback is not None:
            callback(_attempt_observation())  # type: ignore[operator]
        return _successful_session(run_id)

    monkeypatch.setattr(
        "caribou.control.agent_workload.run_agent_session",
        fake_run_agent_session,
    )

    result = execute_agent_workload(store, run_id, adapter=adapter)

    assert result is not None and result.succeeded is True
    assert len(observed_callbacks) == 1
    manifest = store.artifact_manifest(run_id)
    provider_artifacts = [
        artifact
        for artifact in manifest.artifacts
        if artifact.role == "provider_call_receipt"
    ]
    assert len(provider_artifacts) == expected_provider_receipts
    if adapter == CARIBOU_AGENT_ADAPTER:
        assert observed_callbacks[0] is not None
        assert backend.started is True
        assert backend.stopped is True
    else:
        assert observed_callbacks[0] is None


def test_workload_propagates_frozen_max_output_tokens_to_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, run_id, spec = _active_store(
        tmp_path,
        CARIBOU_AGENT_ADAPTER,
        max_output_tokens=8192,
    )
    system = AgentSystem(
        global_policy="policy",
        agents={
            "analyst": Agent(
                name="analyst",
                prompt="analyze",
                commands={},
                code_samples={},
            )
        },
    )
    monkeypatch.setattr(
        AgentSystem,
        "load_from_json",
        classmethod(lambda cls, path: system),
    )
    monkeypatch.setattr(
        "caribou.control.agent_workload._verify_code_identity",
        lambda expected, selected_adapter: None,
    )
    backend = _NonExecutingBackend()
    monkeypatch.setattr(
        "caribou.control.agent_workload._real_sandbox",
        lambda *args, **kwargs: backend,
    )
    provider_calls: list[tuple[str, dict[str, object]]] = []

    def fake_provider_client(
        provider: str, parameters: dict[str, object]
    ) -> object:
        provider_calls.append((provider, parameters))
        return object()

    monkeypatch.setattr(
        "caribou.control.agent_workload._provider_client",
        fake_provider_client,
    )
    runner_kwargs: dict[str, object] = {}

    def fake_run_agent_session(**kwargs: object) -> AgentSessionResult:
        runner_kwargs.update(kwargs)
        return _successful_session(run_id)

    monkeypatch.setattr(
        "caribou.control.agent_workload.run_agent_session",
        fake_run_agent_session,
    )

    result = execute_agent_workload(
        store,
        run_id,
        adapter=CARIBOU_AGENT_ADAPTER,
    )

    assert result is not None and result.succeeded is True
    assert spec.conditions[0].model.parameters == {"max_output_tokens": 8192}
    assert provider_calls == [
        ("deepseek", {"max_output_tokens": 8192}),
    ]
    assert runner_kwargs["max_output_tokens"] == 8192
