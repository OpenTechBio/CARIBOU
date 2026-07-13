"""Cross-record integrity validation for experiment evidence graphs."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence, TypeVar, cast

from .enums import CheckpointStatus, EventType
from .models import (
    Aggregate,
    Artifact,
    ArtifactCreatedPayload,
    BudgetRecordedPayload,
    BudgetRecord,
    Checkpoint,
    CheckpointCreatedPayload,
    Event,
    Experiment,
    ExperimentSpec,
    FailureRecord,
    FailureRecordedPayload,
    MetricRecord,
    MetricRecordedPayload,
    Run,
    checkpoint_integrity_hash,
)
from .serialization import model_hash, validate_run_event_pair


class GraphIntegrityError(ValueError):
    """One or more linked domain records are inconsistent."""


RecordT = TypeVar("RecordT")


def _cycles(
    index: Mapping[str, object], parent_attribute: str, label: str
) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited:
            return
        if identifier in visiting:
            errors.append(f"{label} lineage contains a cycle at {identifier}")
            return
        visiting.add(identifier)
        record = index[identifier]
        parents = getattr(record, parent_attribute)
        if parents is None:
            parent_ids = []
        elif isinstance(parents, str):
            parent_ids = [parents]
        else:
            parent_ids = list(parents)
        for parent in parent_ids:
            if parent in index:
                visit(parent)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in index:
        visit(identifier)
    return errors


def _index(
    records: Iterable[RecordT], attribute: str, label: str
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        identifier = cast(str, getattr(record, attribute))
        if identifier in result:
            raise GraphIntegrityError(f"duplicate {label} ID {identifier}")
        result[identifier] = record
    return result


def validate_record_graph(
    *,
    spec: ExperimentSpec,
    experiment: Experiment,
    runs: Sequence[Run],
    events: Sequence[Event] = (),
    artifacts: Sequence[Artifact] = (),
    failures: Sequence[FailureRecord] = (),
    metrics: Sequence[MetricRecord] = (),
    checkpoints: Sequence[Checkpoint] = (),
    budgets: Sequence[BudgetRecord] = (),
    aggregates: Sequence[Aggregate] = (),
) -> None:
    """Fail closed when IDs, immutable configuration, or evidence links drift."""

    errors: list[str] = []
    expected_hash = model_hash(spec)
    if (
        experiment.spec_id != spec.spec_id
        or experiment.spec_version != spec.spec_version
    ):
        errors.append(
            "experiment does not reference the supplied specification version"
        )
    if experiment.spec_hash != expected_hash:
        errors.append(
            "experiment specification hash does not match canonical specification bytes"
        )

    try:
        run_by_id = _index(runs, "run_id", "run")
        event_by_id = _index(events, "event_id", "event")
        artifact_by_id = _index(artifacts, "artifact_id", "artifact")
        failure_by_id = _index(failures, "failure_id", "failure")
        metric_by_id = _index(metrics, "metric_record_id", "metric")
        checkpoint_by_id = _index(checkpoints, "checkpoint_id", "checkpoint")
        budget_by_id = _index(budgets, "budget_record_id", "budget")
        aggregate_by_id = _index(aggregates, "aggregate_id", "aggregate")
    except GraphIntegrityError as exc:
        errors.append(str(exc))
        run_by_id = {}
        event_by_id = {}
        artifact_by_id = {}
        failure_by_id = {}
        metric_by_id = {}
        checkpoint_by_id = {}
        budget_by_id = {}
        aggregate_by_id = {}

    if set(experiment.run_ids) != set(run_by_id):
        errors.append("experiment run_ids do not exactly match supplied run attempts")
    if set(experiment.aggregate_ids) != set(aggregate_by_id):
        errors.append(
            "experiment aggregate_ids do not exactly match supplied aggregates"
        )

    conditions = {condition.condition_id: condition for condition in spec.conditions}
    events_by_run: dict[str, list[Event]] = defaultdict(list)
    durable_indexes: dict[EventType, tuple[str, Mapping[str, object]]] = {
        EventType.artifact_created: ("artifact_id", artifact_by_id),
        EventType.metric_recorded: ("metric_record_id", metric_by_id),
        EventType.checkpoint_created: ("checkpoint_id", checkpoint_by_id),
        EventType.budget_recorded: ("budget_record_id", budget_by_id),
        EventType.failure_recorded: ("failure_id", failure_by_id),
    }
    for event in events:
        events_by_run[event.run_id].append(event)
    for stream in events_by_run.values():
        stream.sort(key=lambda item: item.sequence)

    for run in runs:
        if (
            run.experiment_id != experiment.experiment_id
            or run.spec_hash != experiment.spec_hash
        ):
            errors.append(
                f"run {run.run_id} belongs to another experiment or specification"
            )
        condition = conditions.get(run.condition_id)
        if condition is None:
            errors.append(f"run {run.run_id} uses unknown condition {run.condition_id}")
        else:
            immutable_pairs = (
                (run.code, spec.code, "code"),
                (run.resolved_model, condition.model, "model"),
                (run.resolved_blueprint, condition.blueprint, "blueprint"),
                (run.resolved_prompt, condition.prompt, "prompt"),
                (run.resolved_memory, condition.memory, "memory"),
                (run.resolved_stop_rules, spec.stop_rules, "stop rules"),
                (run.resolved_budget, spec.budget, "budget"),
                (run.resources, spec.execution.resources, "resources"),
                (run.container, spec.execution.container, "container"),
                (run.executor, spec.execution.executor, "executor"),
            )
            for actual, expected, label in immutable_pairs:
                if actual != expected:
                    errors.append(
                        f"run {run.run_id} resolved {label} drifted from its specification"
                    )
            if run.resolved_inputs != spec.inputs:
                errors.append(
                    f"run {run.run_id} resolved inputs drifted from its specification"
                )
        if run.replicate_index >= spec.repetitions:
            errors.append(f"run {run.run_id} replicate index exceeds the frozen design")
        try:
            validate_run_event_pair(run, events_by_run.get(run.run_id, []))
        except ValueError as exc:
            errors.append(f"run {run.run_id} event ledger: {exc}")

        linked = {
            "artifact": (
                set(run.artifact_ids),
                {
                    key
                    for key, item in artifact_by_id.items()
                    if item.run_id == run.run_id
                },
            ),
            "failure": (
                set(run.failure_ids),
                {
                    key
                    for key, item in failure_by_id.items()
                    if item.run_id == run.run_id
                },
            ),
            "metric": (
                set(run.metric_record_ids),
                {
                    key
                    for key, item in metric_by_id.items()
                    if item.run_id == run.run_id
                },
            ),
            "checkpoint": (
                set(run.checkpoint_ids),
                {
                    key
                    for key, item in checkpoint_by_id.items()
                    if item.run_id == run.run_id
                },
            ),
            "budget": (
                set(run.budget_record_ids),
                {
                    key
                    for key, item in budget_by_id.items()
                    if item.run_id == run.run_id
                },
            ),
        }
        for label, (declared, supplied) in linked.items():
            if declared != supplied:
                errors.append(
                    f"run {run.run_id} {label} links do not match supplied records"
                )

    for event in events:
        if (
            event.run_id not in run_by_id
            or event.experiment_id != experiment.experiment_id
        ):
            errors.append(
                f"event {event.event_id} references an unknown run or experiment"
            )
        payload_reference = durable_indexes.get(event.event_type)
        if payload_reference:
            attribute, index = payload_reference
            if getattr(event.payload, attribute) not in index:
                errors.append(
                    f"event {event.event_id} references a missing durable record"
                )
        if event.causation_event_id is not None:
            cause = event_by_id.get(event.causation_event_id)
            if (
                cause is None
                or cause.run_id != event.run_id
                or cause.sequence >= event.sequence
            ):
                errors.append(f"event {event.event_id} has an invalid causal event")

    for artifact in artifacts:
        producer_event = event_by_id.get(artifact.producer_event_id)
        artifact_run = run_by_id.get(artifact.run_id)
        if (
            producer_event is None
            or artifact_run is None
            or artifact.experiment_id != experiment.experiment_id
            or producer_event.run_id != artifact.run_id
            or producer_event.event_type != EventType.artifact_created
            or not isinstance(producer_event.payload, ArtifactCreatedPayload)
            or producer_event.payload.artifact_id != artifact.artifact_id
            or producer_event.actor != artifact.producer
        ):
            errors.append(
                f"artifact {artifact.artifact_id} has an invalid producer event"
            )
        if artifact_run is not None and artifact.owner != artifact_run.owner:
            errors.append(
                f"artifact {artifact.artifact_id} owner differs from its run owner"
            )
        for parent_id in artifact.parent_artifact_ids:
            parent = artifact_by_id.get(parent_id)
            if parent is None:
                errors.append(
                    f"artifact {artifact.artifact_id} references a missing parent"
                )
            elif (
                parent.experiment_id != artifact.experiment_id
                or parent.owner != artifact.owner
            ):
                errors.append(
                    f"artifact {artifact.artifact_id} has a cross-boundary parent"
                )
            else:
                parent_event = event_by_id.get(parent.producer_event_id)
                if (
                    producer_event is None
                    or parent_event is None
                    or (
                        parent.run_id == artifact.run_id
                        and parent_event.sequence >= producer_event.sequence
                    )
                    or parent_event.occurred_at >= producer_event.occurred_at
                ):
                    errors.append(
                        f"artifact {artifact.artifact_id} parent does not precede it"
                    )

    for failure in failures:
        if (
            failure.run_id not in run_by_id
            or failure.experiment_id != experiment.experiment_id
        ):
            errors.append(
                f"failure {failure.failure_id} references an unknown run or experiment"
            )
        failure_event = event_by_id.get(failure.event_id)
        if (
            failure_event is None
            or failure_event.run_id != failure.run_id
            or failure_event.event_type != EventType.failure_recorded
            or not isinstance(failure_event.payload, FailureRecordedPayload)
            or failure_event.payload.failure_id != failure.failure_id
        ):
            errors.append(f"failure {failure.failure_id} has an invalid recorded event")
        if failure.caused_by_failure_id is not None:
            causal_failure = failure_by_id.get(failure.caused_by_failure_id)
            causal_event = (
                event_by_id.get(causal_failure.event_id)
                if causal_failure is not None
                else None
            )
            if (
                causal_failure is None
                or causal_failure.experiment_id != failure.experiment_id
                or causal_failure.run_id != failure.run_id
                or failure_event is None
                or causal_event is None
                or causal_event.sequence >= failure_event.sequence
            ):
                errors.append(
                    f"failure {failure.failure_id} has an invalid causal failure"
                )
        if failure.traceback_artifact_id is not None:
            traceback = artifact_by_id.get(failure.traceback_artifact_id)
            if (
                traceback is None
                or traceback.experiment_id != failure.experiment_id
                or traceback.run_id != failure.run_id
            ):
                errors.append(
                    f"failure {failure.failure_id} has an invalid traceback artifact"
                )
        failure_run = run_by_id.get(failure.run_id)
        if failure_run is not None and failure.attempt != failure_run.attempt_index:
            errors.append(
                f"failure {failure.failure_id} attempt index differs from its run"
            )

    for metric in metrics:
        if (
            metric.run_id not in run_by_id
            or metric.experiment_id != experiment.experiment_id
        ):
            errors.append(
                f"metric {metric.metric_record_id} references an unknown run or experiment"
            )
        if any(
            identifier not in artifact_by_id for identifier in metric.input_artifact_ids
        ):
            errors.append(
                f"metric {metric.metric_record_id} references missing input artifacts"
            )
        if metric.failure_id is not None:
            metric_failure = failure_by_id.get(metric.failure_id)
            if metric_failure is None or metric_failure.run_id != metric.run_id:
                errors.append(
                    f"metric {metric.metric_record_id} has an invalid failure"
                )
        metric_event = event_by_id.get(metric.event_id)
        if (
            metric_event is None
            or metric_event.run_id != metric.run_id
            or metric_event.event_type != EventType.metric_recorded
            or not isinstance(metric_event.payload, MetricRecordedPayload)
            or metric_event.payload.metric_record_id != metric.metric_record_id
        ):
            errors.append(
                f"metric {metric.metric_record_id} has an invalid recorded event"
            )
        metric_run = run_by_id.get(metric.run_id)
        if metric_run is not None and (
            metric.condition_id != metric_run.condition_id
            or metric.replicate_index != metric_run.replicate_index
        ):
            errors.append(
                f"metric {metric.metric_record_id} design coordinates differ from its run"
            )

    for checkpoint in checkpoints:
        checkpoint_run = run_by_id.get(checkpoint.run_id)
        if (
            checkpoint_run is None
            or checkpoint.experiment_id != experiment.experiment_id
        ):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} references an unknown run"
            )
            continue
        required_artifacts = [
            checkpoint.artifact_manifest_id,
            checkpoint.dataset_artifact_id,
            checkpoint.message_history_artifact_id,
            checkpoint.agent_state_artifact_id,
            checkpoint.executed_actions_artifact_id,
            checkpoint.random_state_artifact_id,
        ]
        if any(
            identifier not in artifact_by_id
            for identifier in required_artifacts
            if identifier
        ):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} references missing artifacts"
            )
        if (
            checkpoint.spec_hash != checkpoint_run.spec_hash
            or checkpoint.code_commit != checkpoint_run.code.commit
        ):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} is incompatible with its run"
            )
        if checkpoint.container_digest != checkpoint_run.container.image.content_hash:
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} container digest is incompatible"
            )
        expected_model = (
            f"{checkpoint_run.resolved_model.provider}:"
            f"{checkpoint_run.resolved_model.model}"
        )
        if checkpoint.model_identity != expected_model:
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} model identity is incompatible"
            )
        if checkpoint.status != CheckpointStatus.complete:
            errors.append(f"checkpoint {checkpoint.checkpoint_id} is not complete")
        if (
            checkpoint.event_sequence > checkpoint_run.event_sequence
            or checkpoint.turn > checkpoint_run.current_turn
        ):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} cursor is ahead of its run"
            )
        if checkpoint.integrity_hash != checkpoint_integrity_hash(checkpoint):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} integrity hash is invalid"
            )
        parent_checkpoint_id = checkpoint.parent_checkpoint_id
        if parent_checkpoint_id is not None:
            parent_checkpoint = checkpoint_by_id.get(parent_checkpoint_id)
            if (
                parent_checkpoint is None
                or parent_checkpoint.run_id != checkpoint.run_id
                or parent_checkpoint.event_sequence >= checkpoint.event_sequence
            ):
                errors.append(
                    f"checkpoint {checkpoint.checkpoint_id} has a missing parent"
                )
        checkpoint_event = event_by_id.get(checkpoint.event_id)
        if (
            checkpoint_event is None
            or checkpoint_event.run_id != checkpoint.run_id
            or checkpoint_event.sequence != checkpoint.event_sequence
            or checkpoint_event.event_type != EventType.checkpoint_created
            or not isinstance(checkpoint_event.payload, CheckpointCreatedPayload)
            or checkpoint_event.payload.checkpoint_id != checkpoint.checkpoint_id
        ):
            errors.append(
                f"checkpoint {checkpoint.checkpoint_id} has an invalid recorded event"
            )
        for artifact_id in required_artifacts:
            if not artifact_id:
                continue
            checkpoint_artifact = artifact_by_id.get(artifact_id)
            if checkpoint_artifact is not None and (
                checkpoint_artifact.experiment_id != checkpoint.experiment_id
                or checkpoint_artifact.run_id != checkpoint.run_id
                or checkpoint_artifact.owner != checkpoint_run.owner
            ):
                errors.append(
                    f"checkpoint {checkpoint.checkpoint_id} crosses an artifact boundary"
                )

    for record in budgets:
        if record.experiment_id != experiment.experiment_id:
            errors.append(
                f"budget {record.budget_record_id} references another experiment"
            )
        if record.run_id is not None and record.run_id not in run_by_id:
            errors.append(f"budget {record.budget_record_id} references an unknown run")
        if record.run_id is not None:
            budget_event = (
                event_by_id.get(record.event_id)
                if record.event_id is not None
                else None
            )
            if (
                budget_event is None
                or budget_event.run_id != record.run_id
                or budget_event.event_type != EventType.budget_recorded
                or not isinstance(budget_event.payload, BudgetRecordedPayload)
                or budget_event.payload.budget_record_id != record.budget_record_id
            ):
                errors.append(
                    f"budget {record.budget_record_id} has an invalid recorded event"
                )

    for aggregate in aggregates:
        if (
            aggregate.experiment_id != experiment.experiment_id
            or aggregate.spec_hash != experiment.spec_hash
        ):
            errors.append(
                f"aggregate {aggregate.aggregate_id} references another study"
            )
        if any(
            identifier not in run_by_id
            for identifier in (*aggregate.included_run_ids, *aggregate.excluded_run_ids)
        ):
            errors.append(
                f"aggregate {aggregate.aggregate_id} references an unknown run"
            )
        if any(
            identifier not in metric_by_id for identifier in aggregate.metric_record_ids
        ):
            errors.append(
                f"aggregate {aggregate.aggregate_id} references a missing metric"
            )
        if any(
            identifier not in artifact_by_id for identifier in aggregate.artifact_ids
        ):
            errors.append(
                f"aggregate {aggregate.aggregate_id} references a missing artifact"
            )

    errors.extend(_cycles(artifact_by_id, "parent_artifact_ids", "artifact"))
    errors.extend(_cycles(failure_by_id, "caused_by_failure_id", "failure"))
    errors.extend(_cycles(checkpoint_by_id, "parent_checkpoint_id", "checkpoint"))

    if errors:
        raise GraphIntegrityError("; ".join(errors))
