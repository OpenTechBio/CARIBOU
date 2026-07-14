"""Fresh-process acceptance for durable agent checkpoint and resume."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .test_cli_agent_path import write_agent_spec
from .test_cli_discovery import response, run_cli
from .test_cli_local_lifecycle import event_lines, submit, wait_for_terminal


def _wait_for_turn_started(
    tmp_path: Path,
    run_id: str,
    *,
    turn: int,
    timeout: float = 10,
) -> list[dict]:
    deadline = time.monotonic() + timeout
    last_status: dict | None = None
    while time.monotonic() < deadline:
        events = event_lines(tmp_path, run_id)
        if any(
            line["event"]["event_type"] == "heartbeat"
            and line["event"]["stage"] == "agent_turn"
            and line["event"]["turn"] == turn
            and line["event"]["payload"]["message"] == "agent turn started"
            for line in events
        ):
            return events

        status_result = run_cli(tmp_path, "run", "status", run_id, "--json")
        assert status_result.returncode == 0, status_result.stderr
        last_status = response(status_result)
        if last_status["object"]["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "rejected",
            "resumable",
        }:
            break
        time.sleep(0.02)
    raise AssertionError(
        f"run did not durably start logical turn {turn}: {last_status}"
    )


def _authoritative_source_snapshot(
    tmp_path: Path,
    run_id: str,
    artifacts: list[dict],
) -> dict[str, tuple[bytes, str]]:
    run_root = tmp_path / "home" / "experiment_store" / "v1" / "runs" / run_id
    relative_paths = {Path("run-journal.json"), Path("artifacts.json")}
    for artifact in artifacts:
        storage_path = Path(artifact["storage_uri"])
        assert not storage_path.is_absolute()
        assert ".." not in storage_path.parts
        relative_paths.add(storage_path)

    snapshot: dict[str, tuple[bytes, str]] = {}
    for relative_path in sorted(relative_paths):
        path = run_root / relative_path
        content = path.read_bytes()
        snapshot[relative_path.as_posix()] = (
            content,
            "sha256:" + hashlib.sha256(content).hexdigest(),
        )
    return snapshot


def _assert_artifacts_verify(
    tmp_path: Path,
    run_id: str,
) -> list[dict]:
    listed_result = run_cli(tmp_path, "artifact", "list", run_id, "--json")
    assert listed_result.returncode == 0, listed_result.stderr
    listed = response(listed_result)
    assert listed["command"] == "artifact.list"
    assert listed["object"] == {
        "type": "run",
        "id": run_id,
        "state": listed["object"]["state"],
    }
    artifacts = listed["data"]["artifacts"]
    assert listed["data"]["count"] == len(artifacts)

    verified_result = run_cli(tmp_path, "artifact", "verify", run_id, "--json")
    assert verified_result.returncode == 0, verified_result.stderr
    verified = response(verified_result)
    assert verified["command"] == "artifact.verify"
    assert verified["ok"] is True
    assert verified["object"] == {
        "type": "run",
        "id": run_id,
        "state": listed["object"]["state"],
    }
    assert verified["data"] == {
        "verified": len(artifacts),
        "artifact_ids": [artifact["artifact_id"] for artifact in artifacts],
    }
    return artifacts


def _artifact_bytes(
    tmp_path: Path,
    run_id: str,
    artifacts: list[dict],
    role: str,
) -> bytes:
    matches = [artifact for artifact in artifacts if artifact["role"] == role]
    assert len(matches) == 1
    return (
        tmp_path
        / "home"
        / "experiment_store"
        / "v1"
        / "runs"
        / run_id
        / matches[0]["storage_uri"]
    ).read_bytes()


def _logical_agent_events(lines: list[dict]) -> list[tuple[object, ...]]:
    logical: list[tuple[object, ...]] = []
    for line in lines:
        event = line["event"]
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type == "heartbeat" and event["stage"] == "agent_turn":
            logical.append((event_type, event["turn"], payload["message"]))
        elif event_type == "message":
            logical.append(
                (
                    event_type,
                    event["turn"],
                    payload["role"],
                    payload["agent_name"],
                    payload["content"],
                    payload["is_delegation"],
                )
            )
        elif event_type == "agent_switch":
            logical.append(
                (
                    event_type,
                    event["turn"],
                    payload["from_agent"],
                    payload["to_agent"],
                    payload["command"],
                    payload["reason"],
                )
            )
        elif event_type == "code_submitted":
            logical.append(
                (
                    event_type,
                    event["turn"],
                    payload["agent_name"],
                    payload["block_index"],
                    payload["total_blocks"],
                )
            )
        elif event_type == "code_result":
            logical.append((event_type, event["turn"], payload["success"]))
    return logical


def _normalized_message_history(content: bytes) -> dict:
    value = json.loads(content)
    assert value["schema_version"] == "caribou.message_history.v1"
    value.pop("run_id")
    return value


def _normalized_session_result(content: bytes) -> dict:
    value = json.loads(content)
    assert value["schema_version"] == "caribou.agent_session_result.v1"
    for field in ("run_id", "started_at", "ended_at", "duration_seconds"):
        value.pop(field)
    return value


def test_external_agent_checkpoint_resume_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    capabilities_result = run_cli(tmp_path, "capabilities", "--json")
    assert capabilities_result.returncode == 0, capabilities_result.stderr
    capabilities = response(capabilities_result)
    commands = capabilities["data"]["commands"]
    assert commands["run.checkpoint"] == {
        "status": "implemented",
        "mutates": True,
    }
    assert commands["run.checkpoints"] == {
        "status": "implemented",
        "mutates": False,
    }
    assert commands["run.resume"] == {
        "status": "implemented",
        "mutates": True,
    }
    assert "checkpoint" in capabilities["data"]["schema_names"]

    schema_result = run_cli(tmp_path, "schema", "checkpoint", "--json")
    assert schema_result.returncode == 0, schema_result.stderr
    schema_payload = response(schema_result)
    assert schema_payload["object"] == {
        "type": "schema",
        "id": "checkpoint",
        "state": "available",
    }
    checkpoint_schema = schema_payload["data"]["schema"]
    assert checkpoint_schema["properties"]["schema_version"]["const"] == (
        "caribou.checkpoint.v1"
    )
    assert checkpoint_schema["additionalProperties"] is False

    checkpoint_key = "contract-checkpoint-after-turn-two"
    checkpoint_reason = "external agent requested a durable turn-two checkpoint"
    resume_key = "contract-resume-from-turn-two"
    # The checkpoint command is a separate Python process. Keep turn two open
    # long enough for interpreter startup plus the durable request write on a
    # loaded shared filesystem; the acceptance condition is the event boundary,
    # not a sub-second scheduling race.
    specification = write_agent_spec(tmp_path, delay=3.0)
    source_id = submit(tmp_path, specification, "contract-checkpoint-source")["data"][
        "run_ids"
    ][0]

    _wait_for_turn_started(tmp_path, source_id, turn=2)
    checkpoint_result = run_cli(
        tmp_path,
        "run",
        "checkpoint",
        source_id,
        "--idempotency-key",
        checkpoint_key,
        "--reason",
        checkpoint_reason,
        "--json",
    )
    assert checkpoint_result.returncode == 0, checkpoint_result.stderr
    checkpoint_requested = response(checkpoint_result)
    assert checkpoint_requested["command"] == "run.checkpoint"
    assert checkpoint_requested["ok"] is True
    assert checkpoint_requested["object"] == {
        "type": "run",
        "id": source_id,
        "state": "running",
    }
    assert checkpoint_requested["data"]["applied"] is True
    assert checkpoint_requested["data"]["safe_boundary"] == "completed_agent_turn"
    request = checkpoint_requested["data"]["request"]
    assert request["schema_version"] == "caribou.checkpoint_request.v1"
    assert request["run_id"] == source_id
    assert request["actor"] == "cli"
    assert request["reason"] == checkpoint_reason
    assert request["idempotency_key_hash"].startswith("sha256:")
    assert len(request["idempotency_key_hash"]) == 71

    source_terminal = wait_for_terminal(tmp_path, source_id)
    assert source_terminal["command"] == "run.status"
    assert source_terminal["object"] == {
        "type": "run",
        "id": source_id,
        "state": "resumable",
    }
    source_run = source_terminal["data"]["run"]
    assert source_run["attempt_index"] == 1
    assert source_run["current_turn"] == 2
    assert source_run["state"] == "resumable"
    assert source_run["terminal_outcome"] == "interrupted_resumable"
    assert source_run["resume_eligible"] is True
    assert source_run["resumed_from_run_id"] is None
    assert source_run["resume_checkpoint_id"] is None

    checkpoints_result = run_cli(tmp_path, "run", "checkpoints", source_id, "--json")
    assert checkpoints_result.returncode == 0, checkpoints_result.stderr
    checkpoints_payload = response(checkpoints_result)
    assert checkpoints_payload["command"] == "run.checkpoints"
    assert checkpoints_payload["object"] == {
        "type": "run",
        "id": source_id,
        "state": "resumable",
    }
    assert checkpoints_payload["data"]["count"] == 1
    checkpoint = checkpoints_payload["data"]["checkpoints"][0]
    checkpoint_id = checkpoint["checkpoint_id"]
    assert checkpoint["schema_version"] == "caribou.checkpoint.v1"
    assert checkpoint["run_id"] == source_id
    assert checkpoint["experiment_id"] == source_run["experiment_id"]
    assert checkpoint["turn"] == 2
    assert checkpoint["stage"] == "agent_turn_boundary"
    assert checkpoint["status"] == "complete"
    assert len(checkpoint["components"]) == 5
    assert set(checkpoint["components"]) == {
        "dataset_state",
        "message_history",
        "agent_state",
        "executed_actions",
        "artifact_manifest",
    }
    assert source_run["checkpoint_ids"] == [checkpoint_id]

    source_events_before_resume = event_lines(tmp_path, source_id)
    source_cursors = [line["cursor"] for line in source_events_before_resume]
    assert source_cursors == list(range(1, source_cursors[-1] + 1))
    assert source_cursors[-1] == source_terminal["data"]["cursor"]
    source_code_submitted = [
        line
        for line in source_events_before_resume
        if line["event"]["event_type"] == "code_submitted"
    ]
    source_code_results = [
        line
        for line in source_events_before_resume
        if line["event"]["event_type"] == "code_result"
    ]
    assert len(source_code_submitted) == len(source_code_results) == 1
    assert source_code_submitted[0]["event"]["turn"] == 2
    assert source_code_results[0]["event"]["turn"] == 2
    assert (
        source_code_submitted[0]["event"]["payload"]["action_id"]
        == source_code_results[0]["event"]["payload"]["action_id"]
    )
    assert not any(line["event"]["turn"] > 2 for line in source_events_before_resume)

    source_artifacts = _assert_artifacts_verify(tmp_path, source_id)
    assert source_artifacts
    source_snapshot = _authoritative_source_snapshot(
        tmp_path, source_id, source_artifacts
    )

    resume_result = run_cli(
        tmp_path,
        "run",
        "resume",
        source_id,
        "--from-checkpoint",
        "latest",
        "--idempotency-key",
        resume_key,
        "--json",
    )
    assert resume_result.returncode == 0, resume_result.stderr
    resumed = response(resume_result)
    assert resumed["command"] == "run.resume"
    assert resumed["ok"] is True
    child_run = resumed["data"]["child_run"]
    child_id = child_run["run_id"]
    assert resumed["object"] == {
        "type": "run",
        "id": child_id,
        "state": child_run["state"],
    }
    assert resumed["data"]["idempotent_replay"] is False
    assert resumed["data"]["workers_launched"] == 1
    assert resumed["data"]["source_run"] == source_run
    assert resumed["data"]["checkpoint"] == checkpoint
    assert child_run["resumed_from_run_id"] == source_id
    assert child_run["resume_checkpoint_id"] == checkpoint_id
    assert child_run["attempt_index"] == 2

    child_terminal = wait_for_terminal(tmp_path, child_id)
    assert child_terminal["object"] == {
        "type": "run",
        "id": child_id,
        "state": "succeeded",
    }
    child = child_terminal["data"]["run"]
    assert child["state"] == "succeeded"
    assert child["terminal_outcome"] == "succeeded"
    assert child["resume_eligible"] is False
    assert child["resumed_from_run_id"] == source_id
    assert child["resume_checkpoint_id"] == checkpoint_id
    assert child["attempt_index"] == source_run["attempt_index"] + 1 == 2
    assert child["current_turn"] == 3
    assert child["experiment_id"] == source_run["experiment_id"]
    assert child["condition_id"] == source_run["condition_id"]
    assert child["replicate_index"] == source_run["replicate_index"]
    assert child["spec_hash"] == source_run["spec_hash"]

    replay_result = run_cli(
        tmp_path,
        "run",
        "resume",
        source_id,
        "--from-checkpoint",
        "latest",
        "--idempotency-key",
        resume_key,
        "--json",
    )
    assert replay_result.returncode == 0, replay_result.stderr
    replay = response(replay_result)
    assert replay["command"] == "run.resume"
    assert replay["object"] == {
        "type": "run",
        "id": child_id,
        "state": "succeeded",
    }
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["workers_launched"] == 0
    assert replay["data"]["source_run"] == source_run
    assert replay["data"]["checkpoint"] == checkpoint
    assert replay["data"]["child_run"] == child

    child_events = event_lines(tmp_path, child_id)
    child_cursors = [line["cursor"] for line in child_events]
    assert child_cursors == list(range(1, child_cursors[-1] + 1))
    assert child_cursors[-1] == child_terminal["data"]["cursor"]
    child_model_events = [
        line
        for line in child_events
        if line["event"]["event_type"] == "message"
        or (
            line["event"]["event_type"] == "heartbeat"
            and line["event"]["stage"] == "agent_turn"
        )
    ]
    assert child_model_events
    assert child_model_events[0]["event"]["turn"] == 3
    assert child_model_events[0]["event"]["event_type"] == "heartbeat"
    assert child_model_events[0]["event"]["payload"]["message"] == (
        "agent turn started"
    )
    assert not any(
        line["event"]["event_type"] in {"code_submitted", "code_result"}
        for line in child_events
    )
    assert child_events[-1]["event"]["event_type"] == "state_transition"
    assert child_events[-1]["event"]["payload"]["to_state"] == "succeeded"

    child_artifacts = _assert_artifacts_verify(tmp_path, child_id)
    assert {artifact["role"] for artifact in child_artifacts} >= {
        "message_history",
        "agent_session_result",
    }
    source_after_resume_result = run_cli(tmp_path, "run", "status", source_id, "--json")
    assert source_after_resume_result.returncode == 0, source_after_resume_result.stderr
    source_after_resume = response(source_after_resume_result)
    assert source_after_resume["object"] == source_terminal["object"]
    assert source_after_resume["data"] == source_terminal["data"]
    assert event_lines(tmp_path, source_id) == source_events_before_resume
    assert (
        _authoritative_source_snapshot(tmp_path, source_id, source_artifacts)
        == source_snapshot
    )

    comparison_result = run_cli(
        tmp_path,
        "experiment",
        "compare",
        source_run["experiment_id"],
        "--json",
    )
    assert comparison_result.returncode == 0, comparison_result.stderr
    comparison = response(comparison_result)["data"]
    assert comparison["status"] == "complete"
    assert comparison["attempt_count"] == 2
    assert comparison["leaf_run_count"] == 1
    assert comparison["leaf_run_ids"] == [child_id]
    assert comparison["superseded_run_ids"] == [source_id]
    assert comparison["awaiting_resume_run_ids"] == []
    assert comparison["conditions"][0]["outcome_counts"] == {"succeeded": 1}
    source_attempt, child_attempt = comparison["attempts"]
    assert source_attempt["run_id"] == source_id
    assert source_attempt["terminal_outcome"] == "interrupted_resumable"
    assert source_attempt["superseded_by_run_id"] == child_id
    assert child_attempt["run_id"] == child_id
    assert child_attempt["resumed_from_run_id"] == source_id
    assert child_attempt["superseded_by_run_id"] is None

    control_directory = tmp_path / "uninterrupted-control"
    control_directory.mkdir()
    control_specification = write_agent_spec(control_directory)
    control_id = submit(
        tmp_path,
        control_specification,
        "contract-uninterrupted-control",
    )["data"]["run_ids"][0]
    control_terminal = wait_for_terminal(tmp_path, control_id)
    assert control_terminal["object"] == {
        "type": "run",
        "id": control_id,
        "state": "succeeded",
    }
    control_run = control_terminal["data"]["run"]
    assert control_run["current_turn"] == child["current_turn"] == 3
    assert control_run["terminal_outcome"] == child["terminal_outcome"] == ("succeeded")

    control_events = event_lines(tmp_path, control_id)
    assert _logical_agent_events(source_events_before_resume + child_events) == (
        _logical_agent_events(control_events)
    )
    control_artifacts = _assert_artifacts_verify(tmp_path, control_id)
    assert _artifact_bytes(
        tmp_path, source_id, source_artifacts, "generated_code"
    ) == _artifact_bytes(tmp_path, control_id, control_artifacts, "generated_code")
    assert _artifact_bytes(
        tmp_path, source_id, source_artifacts, "code_stdout"
    ) == _artifact_bytes(tmp_path, control_id, control_artifacts, "code_stdout")
    assert _normalized_message_history(
        _artifact_bytes(tmp_path, child_id, child_artifacts, "message_history")
    ) == _normalized_message_history(
        _artifact_bytes(tmp_path, control_id, control_artifacts, "message_history")
    )
    assert _normalized_session_result(
        _artifact_bytes(tmp_path, child_id, child_artifacts, "agent_session_result")
    ) == _normalized_session_result(
        _artifact_bytes(
            tmp_path,
            control_id,
            control_artifacts,
            "agent_session_result",
        )
    )
    assert (
        _authoritative_source_snapshot(tmp_path, source_id, source_artifacts)
        == source_snapshot
    )
