"""Run the existing CARIBOU agent session through the durable control plane."""

from __future__ import annotations

import mimetypes
import os
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from rich.console import Console

from caribou.config import ENV_FILE
from caribou.control.api import ControlError, ExitCode
from caribou.domain.enums import ArtifactType, EventType, MemoryStrategy, RunState
from caribou.domain.models import (
    AgentSwitchPayload,
    CodeResultPayload,
    CodeSubmittedPayload,
    ContentReference,
    HeartbeatPayload,
    MessagePayload,
    RagPayload,
)
from caribou.domain.serialization import file_hash, sha256_bytes
from caribou.execution.runner import AgentSessionResult, RunnerEvent, run_agent_session

from .records import ProviderCallReceipt, ProviderCallUsage
from .specs import (
    AGENT_PATH_SMOKE_ADAPTER,
    AGENT_SMOKE_DELAY_PARAMETER,
    CARIBOU_AGENT_ADAPTER,
)
from .store import ExperimentStore


SANDBOX_DATA_PATH = "/workspace/dataset.h5ad"
_SMOKE_CODE = 'print("CARIBOU_AGENT_PATH_OK")'


def _verify_code_identity(expected_commit: str, adapter: str) -> None:
    root_result = subprocess.run(
        [
            "git",
            "-C",
            str(Path(__file__).resolve().parent),
            "rev-parse",
            "--show-toplevel",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if root_result.returncode != 0:
        raise ControlError(
            "CODE_IDENTITY_UNAVAILABLE",
            "agent workloads require a verifiable Git checkout",
            exit_code=ExitCode.integrity,
        )
    repository_root = Path(root_result.stdout.strip())
    head_result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    status_result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if head_result.returncode != 0 or status_result.returncode != 0:
        raise ControlError(
            "CODE_IDENTITY_UNAVAILABLE",
            "the executing Git checkout could not be inspected",
            exit_code=ExitCode.integrity,
        )
    actual = head_result.stdout.strip()
    dirty = bool(status_result.stdout.strip())
    if actual != expected_commit or dirty:
        raise ControlError(
            "CODE_COMMIT_MISMATCH",
            "the executing CARIBOU checkout does not match the frozen clean commit",
            exit_code=ExitCode.integrity,
            details={"expected": expected_commit, "actual": actual, "dirty": dirty},
        )


def _local_file(
    reference: ContentReference, *, role: str, verify_hash: bool = True
) -> Path:
    parsed = urlparse(reference.uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ControlError(
            "CONTENT_URI_UNSUPPORTED",
            f"{role} must use a local file URI",
            exit_code=ExitCode.validation,
            details={"role": role, "uri": reference.uri},
        )
    path = Path(unquote(parsed.path)).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ControlError(
            "CONTENT_FILE_INVALID",
            f"{role} must resolve to a regular non-symlink file",
            exit_code=ExitCode.integrity,
            details={"role": role, "path": str(path)},
        )
    resolved = path.resolve()
    actual_hash = file_hash(resolved) if verify_hash else None
    if actual_hash is not None and actual_hash != reference.content_hash:
        raise ControlError(
            "CONTENT_HASH_MISMATCH",
            f"{role} does not match its frozen SHA-256",
            exit_code=ExitCode.integrity,
            details={
                "role": role,
                "path": str(resolved),
                "expected": reference.content_hash,
                "actual": actual_hash,
            },
        )
    if (
        reference.size_bytes is not None
        and resolved.stat().st_size != reference.size_bytes
    ):
        raise ControlError(
            "CONTENT_SIZE_MISMATCH",
            f"{role} does not match its frozen size",
            exit_code=ExitCode.integrity,
            details={
                "role": role,
                "path": str(resolved),
                "expected": reference.size_bytes,
                "actual": resolved.stat().st_size,
            },
        )
    return resolved


class _ScriptedCompletions:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds
        self._responses = iter(
            (
                "delegate_to_general",
                f"```python\n{_SMOKE_CODE}\n```",
                "end_session",
            )
        )

    def create(self, **_: Any) -> SimpleNamespace:
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        try:
            content = next(self._responses)
        except StopIteration:
            content = "end_session"
        message = SimpleNamespace(content=content, role="assistant")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ScriptedClient:
    def __init__(self, delay_seconds: float) -> None:
        self.chat = SimpleNamespace(completions=_ScriptedCompletions(delay_seconds))


class _RecordingSandbox:
    def __init__(self) -> None:
        self.started = False

    def set_data(self, _: list[tuple[Path, str]], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)

    def start_container(self) -> bool:
        self.started = True
        return True

    def stop_container(self) -> None:
        self.started = False

    def exec_code(self, code: str, timeout: int) -> dict[str, Any]:
        if not self.started:
            raise RuntimeError("scripted sandbox is not running")
        if timeout <= 0 or code.strip() != _SMOKE_CODE:
            raise RuntimeError("scripted sandbox received unexpected code")
        return {
            "status": "ok",
            "stdout": "CARIBOU_AGENT_PATH_OK\n",
            "stderr": "",
            "images": [],
        }


class _CancellationAwareSandbox:
    """Forward the real backend while turning durable cancellation into an event."""

    def __init__(self, backend: Any, should_cancel: Callable[[], bool]) -> None:
        self._backend = backend
        self._should_cancel = should_cancel

    def set_data(self, resources: list[tuple[Path, str]], output_dir: Path) -> None:
        self._backend.set_data(resources, output_dir)

    def start_container(self) -> bool:
        return bool(self._backend.start_container())

    def stop_container(self) -> None:
        self._backend.stop_container()

    def exec_code(self, code: str, timeout: int) -> dict[str, Any]:
        cancel_event = threading.Event()
        watcher_stop = threading.Event()

        def watch() -> None:
            while not watcher_stop.wait(0.05):
                if self._should_cancel():
                    cancel_event.set()
                    return

        if self._should_cancel():
            cancel_event.set()
        watcher = threading.Thread(
            target=watch,
            name="caribou-sandbox-cancel-watch",
            daemon=True,
        )
        watcher.start()
        try:
            result = self._backend.exec_code(
                code,
                timeout=timeout,
                cancel_event=cancel_event,
            )
            if not isinstance(result, dict):
                raise RuntimeError("sandbox returned a non-object result")
            return result
        finally:
            watcher_stop.set()
            watcher.join(timeout=1)


def _verify_blueprint_dependencies(agent_system: Any, run: Any) -> None:
    actual_samples: dict[str, str] = {}
    rag_enabled_agents: list[str] = []
    for agent_name, agent in agent_system.get_all_agents().items():
        if agent.is_rag_enabled:
            rag_enabled_agents.append(str(agent_name))
        for filename, content in agent.code_samples.items():
            digest = sha256_bytes(content.encode("utf-8"))
            previous = actual_samples.get(str(filename))
            if previous is not None and previous != digest:
                raise ControlError(
                    "CODE_SAMPLE_AMBIGUOUS",
                    "agents loaded different content under one code-sample name",
                    exit_code=ExitCode.integrity,
                    details={"filename": str(filename)},
                )
            actual_samples[str(filename)] = digest
    expected_samples = dict(run.resolved_blueprint.code_sample_hashes)
    if actual_samples != expected_samples:
        raise ControlError(
            "CODE_SAMPLE_HASH_MISMATCH",
            "loaded code samples do not match the frozen blueprint manifest",
            exit_code=ExitCode.integrity,
            details={
                "expected": expected_samples,
                "actual": actual_samples,
            },
        )
    if rag_enabled_agents or run.resolved_blueprint.rag_corpus is not None:
        raise ControlError(
            "RAG_NOT_BOUND",
            "the initial real agent workload does not yet bind RAG to its frozen corpus",
            exit_code=ExitCode.validation,
            details={"rag_enabled_agents": sorted(rag_enabled_agents)},
        )


def _provider_client(provider: str, parameters: dict[str, Any]) -> object:
    load_dotenv(dotenv_path=ENV_FILE, override=False)
    unsupported_parameters = sorted(parameters)
    if unsupported_parameters:
        raise ControlError(
            "MODEL_PARAMETERS_UNSUPPORTED",
            "the initial real agent workload cannot silently ignore model parameters",
            exit_code=ExitCode.validation,
            details={"parameters": unsupported_parameters},
        )
    if provider == "openai":
        from openai import OpenAI

        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        return OpenAI(api_key=key, max_retries=0)
    if provider == "deepseek":
        from openai import OpenAI

        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        return OpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
        )
    raise RuntimeError(f"unsupported provider: {provider}")


def _receipt_text(observation: dict[str, object], key: str) -> str | None:
    value = observation.get(key)
    return value if isinstance(value, str) and value else None


def _required_receipt_text(observation: dict[str, object], key: str) -> str:
    value = _receipt_text(observation, key)
    if value is None:
        raise RuntimeError(f"provider observation field {key!r} is not text")
    return value


def _receipt_int(observation: dict[str, object], key: str) -> int | None:
    value = observation.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_receipt_int(observation: dict[str, object], key: str) -> int:
    value = _receipt_int(observation, key)
    if value is None:
        raise RuntimeError(f"provider observation field {key!r} is not an integer")
    return value


def _receipt_datetime(observation: dict[str, object], key: str) -> datetime:
    value = _required_receipt_text(observation, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(
            f"provider observation field {key!r} is not a timestamp"
        ) from exc
    return parsed


def _provider_receipt_recorder(
    store: ExperimentStore,
    run_id: str,
) -> Callable[[dict[str, object]], None]:
    run = store.run(run_id)

    def record(observation: dict[str, object]) -> None:
        turn = _required_receipt_int(observation, "turn")
        attempt = _required_receipt_int(observation, "attempt")
        maximum_attempts = _required_receipt_int(observation, "maximum_attempts")
        requested_model = _required_receipt_text(observation, "requested_model")
        if requested_model != run.resolved_model.model:
            raise RuntimeError(
                "provider observation requested model differs from frozen run"
            )
        if maximum_attempts != run.resolved_stop_rules.retry.maximum_attempts:
            raise RuntimeError(
                "provider observation retry policy differs from frozen run"
            )
        receipt = ProviderCallReceipt(
            call_id=(f"{run_id}:turn:{turn}:attempt:{attempt}"),
            run_id=run_id,
            turn=turn,
            agent_name=_required_receipt_text(observation, "agent_name"),
            attempt=attempt,
            maximum_attempts=maximum_attempts,
            provider=run.resolved_model.provider,
            requested_model=requested_model,
            outcome=_required_receipt_text(observation, "outcome"),  # type: ignore[arg-type]
            started_at=_receipt_datetime(observation, "started_at"),
            ended_at=_receipt_datetime(observation, "ended_at"),
            duration_ms=_required_receipt_int(observation, "duration_ms"),
            response_id=_receipt_text(observation, "response_id"),
            request_id=_receipt_text(observation, "request_id"),
            response_model=_receipt_text(observation, "response_model"),
            system_fingerprint=_receipt_text(observation, "system_fingerprint"),
            finish_reason=_receipt_text(observation, "finish_reason"),
            usage=ProviderCallUsage(
                prompt_tokens=_receipt_int(observation, "prompt_tokens"),
                completion_tokens=_receipt_int(observation, "completion_tokens"),
                total_tokens=_receipt_int(observation, "total_tokens"),
                cached_tokens=_receipt_int(observation, "cached_tokens"),
                cache_miss_tokens=_receipt_int(observation, "cache_miss_tokens"),
                reasoning_tokens=_receipt_int(observation, "reasoning_tokens"),
            ),
            failure_type=_receipt_text(observation, "failure_type"),
            http_status_code=_receipt_int(observation, "http_status_code"),
        )
        store.record_idempotent_json_artifact(
            run_id,
            filename=(f"provider-call-turn-{turn}-attempt-{attempt}.json"),
            role="provider_call_receipt",
            value=receipt.model_dump(mode="json"),
            producer="provider-client",
            artifact_type=ArtifactType.manifest,
            schema_type="caribou.provider_call_receipt",
            schema_version_name="v1",
            turn=turn,
            current_agent=receipt.agent_name,
        )

    return record


def _real_sandbox(
    container_path: Path,
    container_hash: str,
    console: Console,
    *,
    gpu_enabled: bool,
) -> object:
    from caribou.core.sandbox_management import init_singularity_exec

    manager_class, _, _, _, _ = init_singularity_exec(
        str(Path(__file__).resolve().parent),
        SANDBOX_DATA_PATH,
        subprocess,
        console,
        sif_path=container_path,
        sif_sha256=container_hash,
        no_pull=True,
        gpu_enabled=gpu_enabled,
        celltypist_cache_enabled=False,
    )
    return manager_class()


def _artifact_type(path: Path) -> ArtifactType:
    suffix = path.suffix.lower()
    if suffix == ".h5ad":
        return ArtifactType.dataset
    if suffix in {".png", ".jpg", ".jpeg", ".svg", ".pdf"}:
        return ArtifactType.plot
    if suffix == ".ipynb":
        return ArtifactType.notebook
    if suffix in {".csv", ".tsv", ".parquet"}:
        return ArtifactType.metric_table
    if suffix in {".md", ".html", ".json"}:
        return ArtifactType.report
    return ArtifactType.other


def _payload_int(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"runner event field {key!r} is not an integer")
    return value


def _event_recorder(
    store: ExperimentStore, run_id: str
) -> Callable[[RunnerEvent], None]:
    def record(event: RunnerEvent) -> None:
        event_type = event["event_type"]
        turn = event["turn"]
        agent_name = event["agent_name"]
        payload = event["payload"]
        if event_type == "turn_started":
            store.append_run_event(
                run_id,
                event_type=EventType.heartbeat,
                payload=HeartbeatPayload(message="agent turn started"),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
                stage="agent_turn",
            )
            return
        if event_type == "assistant_message":
            store.append_run_event(
                run_id,
                event_type=EventType.message,
                payload=MessagePayload(
                    role=str(payload["role"]),
                    agent_name=agent_name,
                    content=str(payload["content"]),
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
            )
            return
        if event_type == "agent_switch":
            to_agent = str(payload["to_agent"])
            store.append_run_event(
                run_id,
                event_type=EventType.agent_switch,
                payload=AgentSwitchPayload(
                    from_agent=str(payload["from_agent"]),
                    to_agent=to_agent,
                    command=str(payload["command"]),
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=to_agent,
            )
            return
        if event_type == "rag_attempt":
            store.append_run_event(
                run_id,
                event_type=EventType.heartbeat,
                payload=HeartbeatPayload(message="RAG request started"),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
                stage="rag",
            )
            return
        if event_type == "rag_result":
            result_text = str(payload.get("content", "")) or str(
                payload.get("error", "")
            )
            result_artifact_id = None
            if result_text:
                artifact = store.record_text_artifact(
                    run_id,
                    filename=f"turn-{turn}-rag.txt",
                    role="rag_result",
                    text=result_text,
                    producer="agent-runner",
                    artifact_type=ArtifactType.log,
                    turn=turn,
                    current_agent=agent_name,
                )
                result_artifact_id = artifact.artifact_id
            store.append_run_event(
                run_id,
                event_type=EventType.rag,
                payload=RagPayload(
                    query=str(payload["query"]),
                    result_artifact_id=result_artifact_id,
                    success=bool(payload["success"]),
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
            )
            return
        if event_type == "code_submitted":
            block_index = _payload_int(payload, "block_index")
            source = store.record_text_artifact(
                run_id,
                filename=f"turn-{turn}-block-{block_index}.py",
                role="generated_code",
                text=str(payload["source"]),
                producer="agent-runner",
                artifact_type=ArtifactType.code,
                media_type="text/x-python",
                turn=turn,
                current_agent=agent_name,
            )
            store.append_run_event(
                run_id,
                event_type=EventType.code_submitted,
                payload=CodeSubmittedPayload(
                    action_id=str(payload["action_id"]),
                    source_artifact_id=source.artifact_id,
                    agent_name=agent_name,
                    block_index=block_index,
                    total_blocks=_payload_int(payload, "total_blocks"),
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
            )
            return
        if event_type == "code_result":
            block_index = _payload_int(payload, "block_index")
            stdout_id = None
            stderr_id = None
            if payload.get("stdout"):
                stdout = store.record_text_artifact(
                    run_id,
                    filename=f"turn-{turn}-block-{block_index}.stdout.txt",
                    role="code_stdout",
                    text=str(payload["stdout"]),
                    producer="agent-runner",
                    artifact_type=ArtifactType.log,
                    turn=turn,
                    current_agent=agent_name,
                )
                stdout_id = stdout.artifact_id
            if payload.get("stderr"):
                stderr = store.record_text_artifact(
                    run_id,
                    filename=f"turn-{turn}-block-{block_index}.stderr.txt",
                    role="code_stderr",
                    text=str(payload["stderr"]),
                    producer="agent-runner",
                    artifact_type=ArtifactType.log,
                    turn=turn,
                    current_agent=agent_name,
                )
                stderr_id = stderr.artifact_id
            store.append_run_event(
                run_id,
                event_type=EventType.code_result,
                payload=CodeResultPayload(
                    action_id=str(payload["action_id"]),
                    success=bool(payload["success"]),
                    duration_ms=_payload_int(payload, "duration_ms"),
                    stdout_artifact_id=stdout_id,
                    stderr_artifact_id=stderr_id,
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
            )
            return
        if event_type == "session_end":
            store.append_run_event(
                run_id,
                event_type=EventType.heartbeat,
                payload=HeartbeatPayload(
                    message=f"agent session ended: {payload['end_reason']}"
                ),
                actor="agent-runner",
                turn=turn,
                current_agent=agent_name,
                stage="agent_session_end",
            )
            return
        raise RuntimeError(f"unsupported runner event: {event_type}")

    return record


def execute_agent_workload(
    store: ExperimentStore,
    run_id: str,
    *,
    adapter: str,
    actor: str = "local-worker",
) -> AgentSessionResult | None:
    """Execute one frozen agent condition; ``None`` means pre-run cancellation."""

    from caribou.agents.AgentSystem import AgentSystem

    run = store.run(run_id)
    _verify_code_identity(run.code.commit, adapter)
    blueprint_path = _local_file(run.resolved_blueprint.source, role="blueprint")
    prompt_path = _local_file(run.resolved_prompt, role="prompt")
    input_path = _local_file(run.resolved_inputs[0], role="input")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("frozen analysis prompt is empty")

    agent_system = AgentSystem.load_from_json(str(blueprint_path))
    driver = agent_system.get_agent(run.resolved_blueprint.driver_agent)
    if driver is None:
        raise RuntimeError(
            f"driver agent not found: {run.resolved_blueprint.driver_agent}"
        )
    if adapter == CARIBOU_AGENT_ADAPTER:
        _verify_blueprint_dependencies(agent_system, run)

    condition = next(
        item
        for item in store.spec(run.experiment_id).conditions
        if item.condition_id == run.condition_id
    )
    if adapter == AGENT_PATH_SMOKE_ADAPTER:
        if "delegate_to_general" not in driver.commands:
            raise RuntimeError("agent path smoke blueprint lacks delegate_to_general")
        delay_value = condition.parameters.get(AGENT_SMOKE_DELAY_PARAMETER, 0.0)
        if isinstance(delay_value, bool) or not isinstance(delay_value, (int, float)):
            raise RuntimeError("validated agent smoke delay is not numeric")
        delay = float(delay_value)
        llm_client: object = _ScriptedClient(delay)
        sandbox: object = _RecordingSandbox()
        llm_attempt_callback: Callable[[dict[str, object]], None] | None = None
    else:
        # The backend verifies this large image immediately before launch; avoid
        # reading a multi-gigabyte SIF twice in the same worker.
        container_path = _local_file(
            run.container.image, role="container", verify_hash=False
        )
        llm_client = _provider_client(
            run.resolved_model.provider,
            dict(run.resolved_model.parameters),
        )
        llm_attempt_callback = _provider_receipt_recorder(store, run_id)
        sandbox = _CancellationAwareSandbox(
            _real_sandbox(
                container_path,
                run.container.image.content_hash,
                Console(stderr=True),
                gpu_enabled=run.container.gpu_enabled,
            ),
            lambda: store.cancel_requested(run_id),
        )

    output_dir = store.run_dir(run_id) / "workload-output"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox.set_data([(input_path, SANDBOX_DATA_PATH)], output_dir)  # type: ignore[attr-defined]
    if store.cancel_requested(run_id):
        return None
    result: AgentSessionResult | None = None
    history = [
        {
            "role": "system",
            "content": f"**GLOBAL POLICY**: {agent_system.global_policy}\n",
        },
        {
            "role": "system",
            "content": (
                driver.get_full_prompt(None)
                + f"\n\nPrimary dataset path: **{SANDBOX_DATA_PATH}**\n"
                + "Save generated files under `/workspace/outputs/`."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        if not sandbox.start_container():  # type: ignore[attr-defined]
            raise RuntimeError("agent sandbox failed to start")
        if store.cancel_requested(run_id):
            return None
        store.transition_run(
            run_id,
            RunState.running,
            reason="CARIBOU agent workload initialized",
            actor=actor,
        )
        result = run_agent_session(
            console=Console(stderr=True),
            agent_system=agent_system,
            driver_agent=driver,
            analysis_context=(
                f"Primary dataset path: **{SANDBOX_DATA_PATH}**\n"
                "Save generated files under `/workspace/outputs/`."
            ),
            llm_client=llm_client,
            sandbox_manager=sandbox,  # type: ignore[arg-type]
            history=history,
            is_auto=True,
            compress_memory=run.resolved_memory.strategy == MemoryStrategy.episodic,
            agent_report_memory=(
                run.resolved_memory.strategy == MemoryStrategy.agent_report
            ),
            max_turns=run.resolved_stop_rules.maximum_turns,
            model_name=run.resolved_model.model,
            output_dir=output_dir,
            durable_run_id=run_id,
            should_cancel=lambda: store.cancel_requested(run_id),
            event_callback=_event_recorder(store, run_id),
            llm_attempt_callback=llm_attempt_callback,
            timeout_seconds=run.resolved_stop_rules.timeout_seconds,
            max_consecutive_no_action=(
                run.resolved_stop_rules.maximum_consecutive_no_action
            ),
            max_consecutive_exec_failures=(
                run.resolved_stop_rules.maximum_consecutive_execution_failures
            ),
            llm_retry_attempts=run.resolved_stop_rules.retry.maximum_attempts,
            llm_retry_base_delay=(run.resolved_stop_rules.retry.base_delay_seconds),
            llm_retry_max_delay=(run.resolved_stop_rules.retry.maximum_delay_seconds),
        )
        store.record_json_artifact(
            run_id,
            filename="message-history.json",
            role="message_history",
            value={
                "schema_version": "caribou.message_history.v1",
                "run_id": run_id,
                "messages": history,
            },
            producer="agent-runner",
            artifact_type=ArtifactType.message_history,
            schema_type="caribou.message_history",
            schema_version_name="v1",
            turn=result.final_turn,
            current_agent=result.current_agent_name,
        )
        store.record_json_artifact(
            run_id,
            filename="agent-session-result.json",
            role="agent_session_result",
            value=asdict(result),
            producer="agent-runner",
            artifact_type=ArtifactType.report,
            schema_type="caribou.agent_session_result",
            schema_version_name="v1",
            turn=result.final_turn,
            current_agent=result.current_agent_name,
        )
        for path in sorted(output_dir.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(output_dir)
            safe_name = "__".join(relative.parts)
            store.record_file_artifact(
                run_id,
                source=path,
                filename=safe_name,
                role=f"analysis_output:{relative.as_posix()}",
                producer="agent-runner",
                artifact_type=_artifact_type(path),
                media_type=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                turn=result.final_turn,
                current_agent=result.current_agent_name,
            )
        return result
    finally:
        sandbox.stop_container()  # type: ignore[attr-defined]
