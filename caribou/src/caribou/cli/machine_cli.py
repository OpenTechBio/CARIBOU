"""Non-interactive CLI adapters for software agents and automation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import typer
from pydantic import BaseModel

from caribou.control.api import (
    ControlError,
    ExitCode,
    emit_json,
    fail_json,
    machine_response,
)
from caribou.control.service import ExperimentService
from caribou.control.specs import (
    build_local_plan,
    load_experiment_spec,
    validate_control_spec,
)
from caribou.control.store import default_store_root
from caribou.domain.models import (
    Aggregate,
    Artifact,
    BudgetRecord,
    Checkpoint,
    Event,
    Experiment,
    ExperimentSpec,
    FailureRecord,
    MetricRecord,
    Run,
)
from caribou.domain.serialization import model_hash


experiment_app = typer.Typer(
    name="experiment",
    help="Validate, plan, submit, and compare durable experiments.",
    no_args_is_help=True,
)
artifact_app = typer.Typer(
    name="artifact",
    help="List, fetch, and verify durable run artifacts.",
    no_args_is_help=True,
)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "experiment": ExperimentSpec,
    "experiment-record": Experiment,
    "run": Run,
    "event": Event,
    "artifact": Artifact,
    "failure": FailureRecord,
    "metric": MetricRecord,
    "checkpoint": Checkpoint,
    "budget": BudgetRecord,
    "aggregate": Aggregate,
}


def _require_json(json_output: bool) -> None:
    if not json_output:
        typer.echo(
            "This automation command requires --json; no interactive fallback is used.",
            err=True,
        )
        raise typer.Exit(int(ExitCode.usage))


def _machine_call(
    command: str,
    json_output: bool,
    operation: Callable[[], Mapping[str, Any]],
) -> None:
    _require_json(json_output)
    try:
        emit_json(operation())
    except ControlError as exc:
        fail_json(command, exc)
    except Exception as exc:  # pragma: no cover - defensive transport boundary
        fail_json(
            command,
            ControlError(
                "INTERNAL_ERROR",
                "CARIBOU could not complete the command",
                exit_code=ExitCode.internal,
                details={"exception_type": type(exc).__name__},
            ),
        )


def capabilities_command(
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Describe discoverable machine commands and maturity boundaries."""

    def operation() -> Mapping[str, Any]:
        commands = {
            "capabilities": {"status": "implemented", "streaming": False},
            "schema": {"status": "implemented", "streaming": False},
            "experiment.validate": {"status": "implemented", "mutates": False},
            "experiment.plan": {"status": "implemented", "mutates": False},
            "experiment.submit": {"status": "implemented", "mutates": True},
            "run.status": {"status": "implemented", "mutates": False},
            "run.events": {"status": "implemented", "streaming": True},
            "run.cancel": {"status": "implemented", "mutates": True},
            "artifact.list": {"status": "implemented", "mutates": False},
            "artifact.fetch": {"status": "implemented", "mutates": True},
            "artifact.verify": {"status": "implemented", "mutates": False},
            "scheduler.inspect": {"status": "planned", "milestone": "M3"},
        }
        return machine_response(
            "capabilities",
            object_type="caribou",
            object_id="caribou",
            state="available",
            data={
                "machine_response_schema": "caribou.machine_response.v1",
                "stream_schema": "domain Event JSON Lines",
                "stable_exit_codes": {
                    name: int(value) for name, value in ExitCode.__members__.items()
                },
                "commands": commands,
                "schema_names": sorted(SCHEMA_MODELS),
                "execution_boundaries": {
                    "local_lifecycle_smoke": "validated_control_plane_probe",
                    "scripted_agent_path": (
                        "validated_actual_runner_with_test_boundaries"
                    ),
                    "local_agent_analysis": (
                        "implemented_not_validated_real_provider_container"
                    ),
                    "slurm": "planned",
                },
                "store_root": str(default_store_root()),
            },
            links={
                "experiment_schema": "caribou schema experiment --json",
                "validate": "caribou experiment validate SPEC --json",
                "plan": "caribou experiment plan SPEC --json",
            },
        )

    _machine_call("capabilities", json_output, operation)


def schema_command(
    name: str = typer.Argument(..., help="Versioned schema name."),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Return a checked-in consumer contract from its canonical model."""

    def operation() -> Mapping[str, Any]:
        model = SCHEMA_MODELS.get(name)
        if model is None:
            raise ControlError(
                "SCHEMA_NOT_FOUND",
                f"unknown schema name: {name}",
                exit_code=ExitCode.not_found,
                details={"available": sorted(SCHEMA_MODELS)},
            )
        schema = model.model_json_schema()
        return machine_response(
            "schema",
            object_type="schema",
            object_id=name,
            state="available",
            data={"name": name, "schema": schema},
            links={"capabilities": "caribou capabilities --json"},
        )

    _machine_call("schema", json_output, operation)


@experiment_app.command("validate")
def validate_experiment_command(
    specification: Path = typer.Argument(..., exists=False, resolve_path=False),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Strictly validate a frozen experiment specification without mutation."""

    def operation() -> Mapping[str, Any]:
        spec = load_experiment_spec(specification)
        checks = validate_control_spec(spec)
        return machine_response(
            "experiment.validate",
            object_type="experiment_spec",
            object_id=spec.spec_id,
            state="validated",
            data={
                "spec_hash": model_hash(spec),
                "specification": str(specification.expanduser().resolve()),
                "checks": checks,
            },
            links={
                "plan": f"caribou experiment plan {specification} --json",
                "schema": "caribou schema experiment --json",
            },
        )

    _machine_call("experiment.validate", json_output, operation)


@experiment_app.command("plan")
def plan_experiment_command(
    specification: Path = typer.Argument(..., exists=False, resolve_path=False),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Build a deterministic resource plan without submitting work."""

    def operation() -> Mapping[str, Any]:
        spec = load_experiment_spec(specification)
        plan = build_local_plan(spec)
        return machine_response(
            "experiment.plan",
            object_type="experiment_spec",
            object_id=spec.spec_id,
            state="planned",
            data=plan,
            links={
                "validate": f"caribou experiment validate {specification} --json",
                "submit": f"caribou experiment submit {specification} --idempotency-key KEY --json",
            },
        )

    _machine_call("experiment.plan", json_output, operation)


@experiment_app.command("submit")
def submit_experiment_command(
    specification: Path = typer.Argument(..., exists=False, resolve_path=False),
    idempotency_key: str = typer.Option(
        ..., "--idempotency-key", help="Stable duplicate-protection key."
    ),
    expected_plan_hash: str | None = typer.Option(
        None,
        "--expected-plan-hash",
        help="Reject submission if the deterministic plan changed.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Persist a local experiment and detach one worker per planned attempt."""

    def operation() -> Mapping[str, Any]:
        service = ExperimentService()
        spec = service.validate(specification)
        result = service.submit(
            spec,
            idempotency_key=idempotency_key,
            expected_plan_hash=expected_plan_hash,
        )
        submission = result.submission
        experiment = submission.experiment
        runs = list(submission.runs)
        return machine_response(
            "experiment.submit",
            object_type="experiment",
            object_id=experiment.experiment_id,
            state=experiment.state.value,
            data={
                "experiment": experiment.model_dump(mode="json"),
                "runs": [run.model_dump(mode="json") for run in runs],
                "run_ids": [run.run_id for run in runs],
                "plan_hash": submission.plan["plan_hash"],
                "idempotent_replay": submission.idempotent_replay,
                "workers_launched": sum(item.launched for item in result.launches),
            },
            links={
                "status": f"caribou run status {runs[0].run_id} --json",
                "events": f"caribou run events {runs[0].run_id} --after 0 --format jsonl",
            },
        )

    _machine_call("experiment.submit", json_output, operation)


def run_status_command(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Read one durable attempt snapshot without side effects."""

    def operation() -> Mapping[str, Any]:
        run = ExperimentService().status(run_id)
        return machine_response(
            "run.status",
            object_type="run",
            object_id=run.run_id,
            state=run.state.value,
            data={"run": run.model_dump(mode="json"), "cursor": run.event_sequence},
            links={
                "events": f"caribou run events {run.run_id} --after {run.event_sequence} --format jsonl",
                "artifacts": f"caribou artifact list {run.run_id} --json",
            },
        )

    _machine_call("run.status", json_output, operation)


def run_events_command(
    run_id: str = typer.Argument(...),
    after: int = typer.Option(0, "--after", min=0, help="Exclusive event cursor."),
    limit: int = typer.Option(1000, "--limit", min=1, max=10000),
    output_format: str = typer.Option("jsonl", "--format"),
) -> None:
    """Emit durable events as resumable JSON Lines."""

    if output_format != "jsonl":
        fail_json(
            "run.events",
            ControlError(
                "FORMAT_UNSUPPORTED",
                "run events supports only --format jsonl",
                exit_code=ExitCode.validation,
            ),
        )
    try:
        events = ExperimentService().events(run_id, after=after, limit=limit)
        for event in events:
            emit_json(
                {
                    "schema_version": "caribou.event_line.v1",
                    "run_id": run_id,
                    "cursor": event.sequence,
                    "event": event.model_dump(mode="json"),
                }
            )
    except ControlError as exc:
        fail_json("run.events", exc)


def run_cancel_command(
    run_id: str = typer.Argument(...),
    reason: str = typer.Option("cancel requested by CLI", "--reason"),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Request cooperative cancellation and preserve the attempt record."""

    def operation() -> Mapping[str, Any]:
        run, applied = ExperimentService().cancel(run_id, reason=reason)
        return machine_response(
            "run.cancel",
            object_type="run",
            object_id=run.run_id,
            state=run.state.value,
            data={"run": run.model_dump(mode="json"), "applied": applied},
            links={"status": f"caribou run status {run.run_id} --json"},
        )

    _machine_call("run.cancel", json_output, operation)


@artifact_app.command("list")
def artifact_list_command(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """List the durable manifest for one run."""

    def operation() -> Mapping[str, Any]:
        service = ExperimentService()
        run = service.status(run_id)
        artifacts = service.artifacts(run_id)
        return machine_response(
            "artifact.list",
            object_type="run",
            object_id=run_id,
            state=run.state.value,
            data={
                "artifacts": [item.model_dump(mode="json") for item in artifacts],
                "count": len(artifacts),
            },
            links={"verify": f"caribou artifact verify {run_id} --json"},
        )

    _machine_call("artifact.list", json_output, operation)


@artifact_app.command("verify")
def artifact_verify_command(
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Hash, size, symlink, and root-boundary verify every linked artifact."""

    def operation() -> Mapping[str, Any]:
        service = ExperimentService()
        run = service.status(run_id)
        artifacts = service.verify_artifacts(run_id)
        return machine_response(
            "artifact.verify",
            object_type="run",
            object_id=run_id,
            state=run.state.value,
            data={
                "verified": len(artifacts),
                "artifact_ids": [item.artifact_id for item in artifacts],
            },
            links={"list": f"caribou artifact list {run_id} --json"},
        )

    _machine_call("artifact.verify", json_output, operation)


@artifact_app.command("fetch")
def artifact_fetch_command(
    run_id: str = typer.Argument(...),
    artifact_id: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output", resolve_path=False),
    overwrite: bool = typer.Option(False, "--overwrite"),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON object."),
) -> None:
    """Atomically copy one verified artifact to an explicit destination."""

    def operation() -> Mapping[str, Any]:
        service = ExperimentService()
        run = service.status(run_id)
        artifact, destination = service.fetch_artifact(
            run_id, artifact_id, output, overwrite=overwrite
        )
        return machine_response(
            "artifact.fetch",
            object_type="artifact",
            object_id=artifact.artifact_id,
            state="verified",
            data={
                "run_id": run.run_id,
                "output": str(destination),
                "content_hash": artifact.content_hash,
                "size_bytes": artifact.size_bytes,
            },
            links={"verify": f"caribou artifact verify {run_id} --json"},
        )

    _machine_call("artifact.fetch", json_output, operation)


def register_machine_commands(app: typer.Typer, run_app: typer.Typer) -> None:
    """Attach machine commands without changing interactive command behavior."""

    app.command("capabilities")(capabilities_command)
    app.command("schema")(schema_command)
    app.add_typer(experiment_app, name="experiment")
    app.add_typer(artifact_app, name="artifact")
    run_app.command("status")(run_status_command)
    run_app.command("events")(run_events_command)
    run_app.command("cancel")(run_cancel_command)
