"""External-process acceptance for the actual CARIBOU agent execution path."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import yaml

from caribou.domain.enums import SandboxKind, TopologyKind
from caribou.domain.models import (
    CodeIdentity,
    ContentReference,
    ExperimentSpec,
    ModelSpec,
)
from caribou.domain.serialization import file_hash

from ..unit.test_domain_models import make_spec
from .test_cli_discovery import response, run_cli
from .test_cli_local_lifecycle import event_lines, submit, wait_for_terminal


def _head_commit() -> str:
    result = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parents[3]), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _worktree_dirty() -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(Path(__file__).resolve().parents[3]),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _reference(path: Path, media_type: str) -> ContentReference:
    return ContentReference(
        uri=path.resolve().as_uri(),
        content_hash=file_hash(path),
        size_bytes=path.stat().st_size,
        media_type=media_type,
    )


def write_agent_spec(
    tmp_path: Path, *, delay: float = 0.0, invalid_prompt_hash: bool = False
) -> Path:
    package_blueprint = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "caribou"
        / "agents"
        / "integration_system.json"
    )
    blueprint_path = tmp_path / "integration-system.json"
    blueprint_path.write_bytes(package_blueprint.read_bytes())
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(
        "Exercise the durable CARIBOU agent path.\n", encoding="utf-8"
    )
    input_path = tmp_path / "input.h5ad"
    input_path.write_bytes(b"scripted-agent-path-fixture\n")
    image_path = tmp_path / "offline.fixture"
    image_path.write_bytes(b"recording-sandbox\n")

    base = make_spec()
    blueprint = base.conditions[0].blueprint.model_copy(
        update={
            "source": _reference(blueprint_path, "application/json"),
            "topology": TopologyKind.multi_agent,
            "driver_agent": "master_agent",
        }
    )
    prompt_reference = _reference(prompt_path, "text/plain")
    if invalid_prompt_hash:
        prompt_reference = prompt_reference.model_copy(
            update={"content_hash": "sha256:" + "0" * 64}
        )
    condition = base.conditions[0].model_copy(
        update={
            "blueprint": blueprint,
            "model": ModelSpec(
                provider="scripted",
                model="caribou-agent-path-smoke@v1",
                context_length=8192,
            ),
            "prompt": prompt_reference,
            "parameters": {
                "caribou.execution_adapter": "agent_path_smoke",
                "caribou.agent_smoke_delay_seconds": delay,
            },
        }
    )
    container = base.execution.container.model_copy(
        update={
            "sandbox": SandboxKind.offline,
            "image": _reference(image_path, "application/octet-stream"),
        }
    )
    execution = base.execution.model_copy(
        update={"container": container, "output_root": "runs/agent-path"}
    )
    stop_rules = base.stop_rules.model_copy(update={"maximum_turns": 5})
    candidate = base.model_copy(
        update={
            "code": CodeIdentity(
                repository="OpenTechBio/caribou",
                branch="AddingAgentInterface",
                commit=_head_commit(),
                dirty=_worktree_dirty(),
            ),
            "inputs": [_reference(input_path, "application/x-hdf5")],
            "conditions": [condition],
            "execution": execution,
            "stop_rules": stop_rules,
            "repetitions": 1,
        }
    )
    spec = ExperimentSpec.model_validate_json(candidate.model_dump_json())
    path = tmp_path / "agent-path.yaml"
    path.write_text(
        yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def _wait_for_state(
    tmp_path: Path, run_id: str, state: str, timeout: float = 5
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = run_cli(tmp_path, "run", "status", run_id, "--json")
        assert result.returncode == 0, result.stderr
        last = response(result)
        if last["object"]["state"] == state:
            return last
        if last["object"]["state"] in {"failed", "cancelled", "succeeded"}:
            break
        time.sleep(0.03)
    raise AssertionError(f"run did not reach {state}: {last}")


def test_agent_submit_detach_events_and_artifacts(tmp_path: Path) -> None:
    run_id = submit(tmp_path, write_agent_spec(tmp_path), "agent-path-success")["data"][
        "run_ids"
    ][0]
    terminal = wait_for_terminal(tmp_path, run_id)
    assert terminal["object"]["state"] == "succeeded"

    lines = event_lines(tmp_path, run_id)
    event_types = {line["event"]["event_type"] for line in lines}
    assert {
        "heartbeat",
        "message",
        "agent_switch",
        "code_submitted",
        "code_result",
        "artifact_created",
    } <= event_types
    cursors = [line["cursor"] for line in lines]
    assert cursors == list(range(1, cursors[-1] + 1))

    listed_result = run_cli(tmp_path, "artifact", "list", run_id, "--json")
    assert listed_result.returncode == 0, listed_result.stderr
    artifacts = response(listed_result)["data"]["artifacts"]
    roles = {artifact["role"] for artifact in artifacts}
    assert {
        "generated_code",
        "code_stdout",
        "message_history",
        "agent_session_result",
    } <= roles

    verified = run_cli(tmp_path, "artifact", "verify", run_id, "--json")
    assert verified.returncode == 0, verified.stderr
    assert response(verified)["data"]["verified"] == len(artifacts)

    history_artifact = next(
        artifact for artifact in artifacts if artifact["role"] == "message_history"
    )
    destination = tmp_path / "fetched-history.json"
    fetched = run_cli(
        tmp_path,
        "artifact",
        "fetch",
        run_id,
        history_artifact["artifact_id"],
        "--output",
        str(destination),
        "--json",
    )
    assert fetched.returncode == 0, fetched.stderr
    history = json.loads(destination.read_text(encoding="utf-8"))
    assert history["run_id"] == run_id
    assert any(
        "CARIBOU_AGENT_PATH_OK" in message["content"] for message in history["messages"]
    )


def test_agent_cancellation_is_observed_after_provider_boundary(tmp_path: Path) -> None:
    specification = write_agent_spec(tmp_path, delay=0.8)
    run_id = submit(tmp_path, specification, "agent-path-cancel")["data"]["run_ids"][0]
    _wait_for_state(tmp_path, run_id, "running")
    cancelled = run_cli(
        tmp_path,
        "run",
        "cancel",
        run_id,
        "--reason",
        "external agent cancellation probe",
        "--json",
    )
    assert cancelled.returncode == 0, cancelled.stderr
    terminal = wait_for_terminal(tmp_path, run_id)
    assert terminal["object"]["state"] == "cancelled"

    lines = event_lines(tmp_path, run_id)
    assert any(line["event"]["event_type"] == "heartbeat" for line in lines)
    artifacts = response(run_cli(tmp_path, "artifact", "list", run_id, "--json"))[
        "data"
    ]["artifacts"]
    assert {artifact["role"] for artifact in artifacts} >= {
        "message_history",
        "agent_session_result",
    }


def test_agent_workload_rejects_frozen_content_mismatch(tmp_path: Path) -> None:
    specification = write_agent_spec(tmp_path, invalid_prompt_hash=True)
    run_id = submit(tmp_path, specification, "agent-path-bad-prompt")["data"][
        "run_ids"
    ][0]
    terminal = wait_for_terminal(tmp_path, run_id)
    assert terminal["object"]["state"] == "failed"
    assert terminal["data"]["run"]["end_reason"] == (
        "worker failure: CONTENT_HASH_MISMATCH"
    )
    assert (
        response(run_cli(tmp_path, "artifact", "list", run_id, "--json"))["data"][
            "artifacts"
        ]
        == []
    )
