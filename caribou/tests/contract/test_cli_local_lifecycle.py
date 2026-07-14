"""End-to-end local lifecycle driven only through separate CLI processes."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .test_cli_discovery import response, run_cli, write_spec


TERMINAL_STATES = {"succeeded", "failed", "cancelled", "rejected", "resumable"}


def wait_for_terminal(tmp_path: Path, run_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        result = run_cli(tmp_path, "run", "status", run_id, "--json")
        assert result.returncode == 0, result.stderr
        last = response(result)
        if last["object"]["state"] in TERMINAL_STATES:
            return last
        time.sleep(0.05)
    raise AssertionError(f"run did not reach a terminal state: {last}")


def submit(tmp_path: Path, specification: Path, key: str) -> dict:
    result = run_cli(
        tmp_path,
        "experiment",
        "submit",
        str(specification),
        "--idempotency-key",
        key,
        "--json",
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return response(result)


def event_lines(tmp_path: Path, run_id: str, after: int = 0) -> list[dict]:
    result = run_cli(
        tmp_path,
        "run",
        "events",
        run_id,
        "--after",
        str(after),
        "--format",
        "jsonl",
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_submit_detach_reconnect_events_and_artifact(tmp_path: Path) -> None:
    specification = write_spec(tmp_path, repetitions=1, smoke_seconds=0.15)
    submitted = submit(tmp_path, specification, "contract-success")
    run_id = submitted["data"]["run_ids"][0]
    assert submitted["data"]["idempotent_replay"] is False
    assert submitted["data"]["workers_launched"] == 1

    terminal = wait_for_terminal(tmp_path, run_id)
    assert terminal["object"]["state"] == "succeeded"
    final_cursor = terminal["data"]["cursor"]

    lines = event_lines(tmp_path, run_id)
    assert [line["cursor"] for line in lines] == list(range(1, final_cursor + 1))
    assert lines[-1]["event"]["payload"]["to_state"] == "succeeded"
    saved_cursor = lines[-2]["cursor"]
    resumed = event_lines(tmp_path, run_id, saved_cursor)
    assert [line["cursor"] for line in resumed] == [final_cursor]

    listed_result = run_cli(tmp_path, "artifact", "list", run_id, "--json")
    assert listed_result.returncode == 0, listed_result.stderr
    listed = response(listed_result)
    assert listed["data"]["count"] == 1
    artifact = listed["data"]["artifacts"][0]

    verified_result = run_cli(tmp_path, "artifact", "verify", run_id, "--json")
    assert verified_result.returncode == 0, verified_result.stderr
    assert response(verified_result)["data"]["verified"] == 1

    output = tmp_path / "retrieved" / artifact["filename"]
    fetched_result = run_cli(
        tmp_path,
        "artifact",
        "fetch",
        run_id,
        artifact["artifact_id"],
        "--output",
        str(output),
        "--json",
    )
    assert fetched_result.returncode == 0, fetched_result.stderr
    fetched = response(fetched_result)
    assert fetched["data"]["content_hash"] == artifact["content_hash"]
    assert json.loads(output.read_text())["run_id"] == run_id

    replay = submit(tmp_path, specification, "contract-success")
    assert replay["data"]["run_ids"] == [run_id]
    assert replay["data"]["idempotent_replay"] is True
    assert replay["data"]["workers_launched"] == 0


def test_cancel_after_submit_is_durable_and_idempotent(tmp_path: Path) -> None:
    specification = write_spec(tmp_path, repetitions=1, smoke_seconds=3.0)
    run_id = submit(tmp_path, specification, "contract-cancel")["data"]["run_ids"][0]
    cancelled_result = run_cli(
        tmp_path,
        "run",
        "cancel",
        run_id,
        "--reason",
        "contract cancellation",
        "--json",
    )
    assert cancelled_result.returncode == 0, cancelled_result.stderr
    assert response(cancelled_result)["data"]["applied"] is True
    terminal = wait_for_terminal(tmp_path, run_id)
    assert terminal["object"]["state"] == "cancelled"

    repeated = run_cli(tmp_path, "run", "cancel", run_id, "--json")
    assert repeated.returncode == 0
    payload = response(repeated)
    assert payload["object"]["state"] == "cancelled"
    assert payload["data"]["applied"] is False


def test_checkpoint_rejects_non_agent_workload(tmp_path: Path) -> None:
    specification = write_spec(tmp_path, repetitions=1, smoke_seconds=3.0)
    run_id = submit(tmp_path, specification, "checkpoint-non-agent")["data"]["run_ids"][
        0
    ]

    rejected = run_cli(
        tmp_path,
        "run",
        "checkpoint",
        run_id,
        "--idempotency-key",
        "invalid-lifecycle-checkpoint",
        "--json",
    )

    assert rejected.returncode == 12
    assert response(rejected)["error"]["code"] == "CHECKPOINT_ADAPTER_UNSUPPORTED"
    cancelled = run_cli(tmp_path, "run", "cancel", run_id, "--json")
    assert cancelled.returncode == 0, cancelled.stderr
    assert wait_for_terminal(tmp_path, run_id)["object"]["state"] == "cancelled"


def test_idempotency_conflict_and_plan_guard_are_typed(tmp_path: Path) -> None:
    first = write_spec(tmp_path, repetitions=1)
    submit(tmp_path, first, "same-key")
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    changed = write_spec(changed_dir, repetitions=1, smoke_seconds=0.2)
    conflict = run_cli(
        tmp_path,
        "experiment",
        "submit",
        str(changed),
        "--idempotency-key",
        "same-key",
        "--json",
    )
    assert conflict.returncode == 12
    assert response(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    guarded = run_cli(
        tmp_path,
        "experiment",
        "submit",
        str(first),
        "--idempotency-key",
        "different-key",
        "--expected-plan-hash",
        "sha256:" + "0" * 64,
        "--json",
    )
    assert guarded.returncode == 12
    assert response(guarded)["error"]["code"] == "PLAN_CHANGED"


def test_unknown_run_and_artifact_tampering_fail_closed(tmp_path: Path) -> None:
    missing = run_cli(tmp_path, "run", "status", "run_" + "0" * 32, "--json")
    assert missing.returncode == 11
    assert response(missing)["error"]["code"] == "RUN_NOT_FOUND"

    specification = write_spec(tmp_path, repetitions=1)
    run_id = submit(tmp_path, specification, "tamper")["data"]["run_ids"][0]
    wait_for_terminal(tmp_path, run_id)
    listed = response(run_cli(tmp_path, "artifact", "list", run_id, "--json"))
    artifact = listed["data"]["artifacts"][0]
    stored = (
        tmp_path
        / "home"
        / "experiment_store"
        / "v1"
        / "runs"
        / run_id
        / artifact["storage_uri"]
    )
    stored.write_text("tampered\n")
    result = run_cli(tmp_path, "artifact", "verify", run_id, "--json")
    assert result.returncode == 19
    assert response(result)["error"]["code"] == "ARTIFACT_INTEGRITY_ERROR"


def test_run_id_traversal_and_fetch_symlink_fail_closed(tmp_path: Path) -> None:
    traversal = run_cli(tmp_path, "run", "status", "../../../../outside", "--json")
    assert traversal.returncode == 10
    assert response(traversal)["error"]["code"] == "RUN_ID_INVALID"

    specification = write_spec(tmp_path, repetitions=1)
    run_id = submit(tmp_path, specification, "symlink-destination")["data"]["run_ids"][
        0
    ]
    wait_for_terminal(tmp_path, run_id)
    artifact = response(run_cli(tmp_path, "artifact", "list", run_id, "--json"))[
        "data"
    ]["artifacts"][0]

    victim = tmp_path / "victim.txt"
    victim.write_text("preserve me\n", encoding="utf-8")
    destination = tmp_path / "fetch-output"
    destination.symlink_to(victim)
    fetched = run_cli(
        tmp_path,
        "artifact",
        "fetch",
        run_id,
        artifact["artifact_id"],
        "--output",
        str(destination),
        "--overwrite",
        "--json",
    )
    assert fetched.returncode == 12
    assert response(fetched)["error"]["code"] == "OUTPUT_EXISTS"
    assert victim.read_text(encoding="utf-8") == "preserve me\n"
