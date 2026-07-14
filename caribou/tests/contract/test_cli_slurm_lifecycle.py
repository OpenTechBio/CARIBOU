"""External-process CLI journey through a deterministic fake Slurm boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from caribou.control.specs import ADAPTER_PARAMETER, LOCAL_LIFECYCLE_ADAPTER
from caribou.control.store import ExperimentStore
from caribou.domain.enums import ExecutorKind
from caribou.domain.models import ExperimentSpec

from ..unit.test_domain_models import COMMIT, make_spec


FAKE_JOB_ID = "742"


def _response(result: subprocess.CompletedProcess[str]) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, (result.stdout, result.stderr)
    return json.loads(lines[0])


def _write_fake_slurm(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "fake-slurm-bin"
    fake_bin.mkdir()
    log = tmp_path / "fake-slurm-commands.jsonl"
    terminal = tmp_path / "fake-slurm-terminal"
    dispatcher = fake_bin / "fake-slurm"
    dispatcher.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
job_id = os.environ["FAKE_SLURM_JOB_ID"]
with Path(os.environ["FAKE_SLURM_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([command, *arguments]) + "\\n")

if command == "sbatch":
    expected = ["--parsable", "--hold", "--partition=peerd", "--export=NIL"]
    if arguments[:4] != expected or len(arguments) != 5:
        raise SystemExit(64)
    print(f"{job_id};fake-cluster")
elif command == "scontrol":
    if arguments != ["release", job_id]:
        raise SystemExit(64)
elif command == "scancel":
    if arguments != [job_id]:
        raise SystemExit(64)
elif command == "squeue":
    if "--name" in arguments:
        pass
    elif not Path(os.environ["FAKE_SLURM_TERMINAL"]).exists():
        print(f"{job_id}|PENDING|peerd|(null)|00:00|3|3072M|Priority")
elif command == "sacct":
    if not Path(os.environ["FAKE_SLURM_TERMINAL"]).exists():
        raise SystemExit(1)
    root = (
        f"{job_id}|COMPLETED|0:0|12|3|3072M||node-a|"
        "2026-07-14T01:00:00|2026-07-14T01:00:12|peerd"
    )
    batch = (
        f"{job_id}.batch|COMPLETED|0:0|12|3|3072M|2048K|node-a|"
        "2026-07-14T01:00:00|2026-07-14T01:00:12|peerd"
    )
    print(root)
    print(batch)
else:
    raise SystemExit(64)
""",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    for name in ("sbatch", "scontrol", "scancel", "squeue", "sacct"):
        (fake_bin / name).symlink_to(dispatcher.name)
    return fake_bin, log, terminal


def _environment(
    tmp_path: Path, fake_bin: Path, log: Path, terminal: Path
) -> dict[str, str]:
    source_root = Path(__file__).resolve().parents[2] / "src"
    environment = os.environ.copy()
    environment.update(
        {
            "CARIBOU_HOME": str(tmp_path / "home"),
            "CARIBOU_CODE_COMMIT": COMMIT,
            "FAKE_SLURM_JOB_ID": FAKE_JOB_ID,
            "FAKE_SLURM_LOG": str(log),
            "FAKE_SLURM_TERMINAL": str(terminal),
            "PATH": os.pathsep.join((str(fake_bin), environment["PATH"])),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(source_root), environment.get("PYTHONPATH", "")))
            ),
        }
    )
    return environment


def _run_cli(
    tmp_path: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "caribou.cli.main", *arguments],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _write_spec(tmp_path: Path) -> Path:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={
            "parameters": {
                ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER,
                "caribou.lifecycle_smoke_seconds": 0.0,
            }
        }
    )
    resources = base.execution.resources.model_copy(
        update={
            "cpu_cores": 3,
            "memory_bytes": 3 * 1024**3,
            "wall_seconds": 3661,
        }
    )
    execution = base.execution.model_copy(
        update={
            "executor": ExecutorKind.slurm,
            "partition": "peerd",
            "resources": resources,
        }
    )
    spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={
                "conditions": [condition],
                "execution": execution,
                "repetitions": 1,
            }
        ).model_dump_json()
    )
    path = tmp_path / "slurm-experiment.yaml"
    path.write_text(
        yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def test_external_agent_can_submit_reconnect_execute_reconcile_and_fetch(
    tmp_path: Path,
) -> None:
    fake_bin, log, terminal_marker = _write_fake_slurm(tmp_path)
    environment = _environment(tmp_path, fake_bin, log, terminal_marker)
    specification = _write_spec(tmp_path)

    submitted_result = _run_cli(
        tmp_path,
        environment,
        "experiment",
        "submit",
        str(specification),
        "--idempotency-key",
        "external-slurm-journey",
        "--json",
    )
    assert submitted_result.returncode == 0, (
        submitted_result.stdout,
        submitted_result.stderr,
    )
    submitted = _response(submitted_result)
    run_id = submitted["data"]["run_ids"][0]
    assert submitted["data"]["workers_launched"] == 1
    assert submitted["data"]["runs"][0]["scheduler_job_id"] == FAKE_JOB_ID

    reconnected_result = _run_cli(
        tmp_path, environment, "run", "status", run_id, "--json"
    )
    assert reconnected_result.returncode == 0, reconnected_result.stderr
    reconnected = _response(reconnected_result)
    assert reconnected["object"]["state"] == "queued"
    assert reconnected["data"]["run"]["partition"] == "peerd"
    assert reconnected["data"]["run"]["scheduler_job_id"] == FAKE_JOB_ID

    inspected_result = _run_cli(
        tmp_path, environment, "scheduler", "inspect", run_id, "--json"
    )
    assert inspected_result.returncode == 0, inspected_result.stderr
    inspected = _response(inspected_result)
    assert inspected["object"]["state"] == "pending"
    assert inspected["data"]["observation"]["source"] == "squeue"
    assert inspected["data"]["observation"]["partition"] == "peerd"
    assert inspected["data"]["handle"]["job_id"] == FAKE_JOB_ID
    assert inspected["data"]["handle"]["partition"] == "peerd"
    assert inspected["data"]["handle"]["script_hash"].startswith("sha256:")
    assert inspected["data"]["submission"]["job_name"] == f"caribou_{run_id}"
    assert len(inspected["data"]["submission"]["attempts"]) == 1
    assert inspected["data"]["cancellation"] is None

    store_root = tmp_path / "home" / "experiment_store" / "v1"
    generated_script = store_root / "runs" / run_id / "slurm-job.sh"
    assert generated_script.is_file()
    worker_environment = environment.copy()
    worker_environment.update(
        {
            "SLURM_JOB_ID": FAKE_JOB_ID,
            "SLURM_JOB_PARTITION": "peerd",
        }
    )
    worker = subprocess.run(
        ["bash", str(generated_script)],
        cwd=tmp_path,
        env=worker_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert worker.returncode == 0, (worker.stdout, worker.stderr)
    terminal_marker.write_text("COMPLETED\n", encoding="utf-8")

    completed_result = _run_cli(
        tmp_path, environment, "run", "status", run_id, "--json"
    )
    assert completed_result.returncode == 0, completed_result.stderr
    assert _response(completed_result)["object"]["state"] == "succeeded"

    reconciled_result = _run_cli(
        tmp_path, environment, "scheduler", "reconcile", run_id, "--json"
    )
    assert reconciled_result.returncode == 0, reconciled_result.stderr
    reconciled = _response(reconciled_result)
    assert reconciled["object"]["state"] == "succeeded"
    assert reconciled["data"]["accounting_created"] is True
    assert reconciled["data"]["run_transition_applied"] is False
    assert reconciled["data"]["accounting"]["state"] == "COMPLETED"
    assert reconciled["data"]["accounting"]["consistent_with_run"] is True
    assert reconciled["data"]["accounting"]["raw_output_hash"].startswith(
        "sha256:"
    )

    events_result = _run_cli(
        tmp_path,
        environment,
        "run",
        "events",
        run_id,
        "--after",
        "0",
        "--format",
        "jsonl",
    )
    assert events_result.returncode == 0, events_result.stderr
    events = [json.loads(line) for line in events_result.stdout.splitlines()]
    assert [event["cursor"] for event in events] == list(
        range(1, events[-1]["cursor"] + 1)
    )
    assert any(
        event["event"]["stage"] == "scheduler_submission" for event in events
    )
    assert any(
        event["event"]["event_type"] == "state_transition"
        and event["event"]["payload"]["to_state"] == "succeeded"
        for event in events
    )

    listed_result = _run_cli(
        tmp_path, environment, "artifact", "list", run_id, "--json"
    )
    assert listed_result.returncode == 0, listed_result.stderr
    listed = _response(listed_result)
    artifacts = listed["data"]["artifacts"]
    assert listed["data"]["count"] == len(artifacts)
    by_role = {artifact["role"]: artifact for artifact in artifacts}
    assert {
        "lifecycle_smoke_result",
        "slurm_accounting",
        "slurm_accounting_raw",
        "slurm_job_script",
    } <= set(by_role)
    assert set(by_role) <= {
        "lifecycle_smoke_result",
        "slurm_accounting",
        "slurm_accounting_raw",
        "slurm_job_script",
        "slurm_stdout",
    }
    assert by_role["slurm_accounting_raw"]["content_hash"] == reconciled["data"][
        "accounting"
    ]["raw_output_hash"]
    assert by_role["slurm_job_script"]["content_hash"] == inspected["data"][
        "handle"
    ]["script_hash"]

    verified_result = _run_cli(
        tmp_path, environment, "artifact", "verify", run_id, "--json"
    )
    assert verified_result.returncode == 0, verified_result.stderr
    assert _response(verified_result)["data"]["verified"] == len(artifacts)

    for role in (
        "lifecycle_smoke_result",
        "slurm_accounting",
        "slurm_accounting_raw",
        "slurm_job_script",
    ):
        artifact = by_role[role]
        fetched_path = tmp_path / "fetched" / artifact["filename"]
        fetched_result = _run_cli(
            tmp_path,
            environment,
            "artifact",
            "fetch",
            run_id,
            artifact["artifact_id"],
            "--output",
            str(fetched_path),
            "--json",
        )
        assert fetched_result.returncode == 0, fetched_result.stderr
        fetched = _response(fetched_result)
        assert fetched["data"]["content_hash"] == artifact["content_hash"]
    fetched_script = tmp_path / "fetched" / by_role["slurm_job_script"]["filename"]
    assert fetched_script.read_bytes() == generated_script.read_bytes()
    lifecycle_result = tmp_path / "fetched" / by_role["lifecycle_smoke_result"][
        "filename"
    ]
    assert json.loads(lifecycle_result.read_text(encoding="utf-8"))["run_id"] == run_id

    command_log = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert command_log[0] == [
        "squeue",
        "--noheader",
        "--user",
        str(os.geteuid()),
        "--name",
        f"caribou_{run_id}",
        "--states=PENDING",
        "--format=%i|%j|%P|%r|%U",
    ]
    assert command_log[1][0:5] == [
        "sbatch",
        "--parsable",
        "--hold",
        "--partition=peerd",
        "--export=NIL",
    ]
    assert command_log[2] == ["scontrol", "release", FAKE_JOB_ID]
    assert command_log[3][0] == "squeue"
    assert command_log[-2][0] == "squeue"
    assert command_log[-1][0] == "sacct"


def test_scheduler_inspect_reports_submission_without_handle_and_rejects_local(
    tmp_path: Path,
) -> None:
    fake_bin, log, terminal_marker = _write_fake_slurm(tmp_path)
    environment = _environment(tmp_path, fake_bin, log, terminal_marker)
    slurm_specification = _write_spec(tmp_path)
    slurm_spec = ExperimentSpec.model_validate_json(
        json.dumps(
            yaml.safe_load(slurm_specification.read_text(encoding="utf-8"))
        )
    )
    store = ExperimentStore(tmp_path / "home" / "experiment_store" / "v1")
    slurm_run = store.submit(slurm_spec, "inspect-submission-only").runs[0]
    _, script_hash = store.write_scheduler_script(
        slurm_run.run_id, "#!/bin/bash\nexit 0\n"
    )
    with store.mutation_lock():
        store._record_scheduler_submission_attempt_unlocked(
            run_id=slurm_run.run_id,
            job_name=f"caribou_{slurm_run.run_id}",
            script_hash=script_hash,
        )

    inspected_result = _run_cli(
        tmp_path,
        environment,
        "scheduler",
        "inspect",
        slurm_run.run_id,
        "--json",
    )
    assert inspected_result.returncode == 0, inspected_result.stderr
    inspected = _response(inspected_result)
    assert inspected["object"] == {
        "type": "scheduler_submission",
        "id": slurm_run.run_id,
        "state": "queued",
    }
    assert inspected["data"]["run_id"] == slurm_run.run_id
    assert inspected["data"]["handle"] is None
    assert inspected["data"]["observation"] is None
    assert inspected["data"]["cancellation"] is None
    assert inspected["data"]["submission"]["job_name"] == (
        f"caribou_{slurm_run.run_id}"
    )
    assert len(inspected["data"]["submission"]["attempts"]) == 1

    base = make_spec()
    local_condition = base.conditions[0].model_copy(
        update={
            "parameters": {
                ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER,
                "caribou.lifecycle_smoke_seconds": 0.0,
            }
        }
    )
    local_spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [local_condition], "repetitions": 1}
        ).model_dump_json()
    )
    local_run = store.submit(local_spec, "inspect-local-rejection").runs[0]
    local_result = _run_cli(
        tmp_path,
        environment,
        "scheduler",
        "inspect",
        local_run.run_id,
        "--json",
    )
    assert local_result.returncode == 11
    local_error = _response(local_result)
    assert local_error["error"]["code"] == "SCHEDULER_HANDLE_NOT_FOUND"
