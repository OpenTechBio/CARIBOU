"""Canonical serialization and crash-safe persistence for domain records."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal, Optional, Sequence, Type, TypeVar

import fcntl
from pydantic import BaseModel, Field, ValidationError, model_validator

from .enums import EventType, ExperimentState, RunState
from .lifecycle import (
    EXPERIMENT_TRANSITIONS,
    RUN_TRANSITIONS,
    ExperimentTransitionResult,
    RunTransition,
)
from .models import (
    ArtifactCreatedPayload,
    BudgetRecordedPayload,
    Checkpoint,
    CheckpointCreatedPayload,
    DomainModel,
    Event,
    Experiment,
    ExperimentTransitionRecord,
    FailureRecordedPayload,
    MetricRecordedPayload,
    Run,
    StateTransitionPayload,
    checkpoint_integrity_hash,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class PersistenceError(RuntimeError):
    """Base class for durable-record persistence errors."""


class ConcurrentUpdateError(PersistenceError):
    """The stored record changed after the caller read it."""


class IntegrityError(PersistenceError):
    """Stored bytes or linked records violate their integrity contract."""


class RunJournal(DomainModel):
    """One atomically persisted run snapshot and its complete durable ledger."""

    schema_version: Literal["caribou.run_journal.v1"] = "caribou.run_journal.v1"
    run: Run
    events: list[Event]
    checkpoints: list[Checkpoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot_and_events(self) -> "RunJournal":
        validate_run_event_pair(self.run, self.events)
        checkpoint_ids = [checkpoint.checkpoint_id for checkpoint in self.checkpoints]
        if len(checkpoint_ids) != len(set(checkpoint_ids)):
            raise ValueError("run journal checkpoint IDs must be unique")
        if checkpoint_ids != self.run.checkpoint_ids:
            raise ValueError(
                "run journal checkpoints must match the run checkpoint IDs"
            )
        event_by_id = {event.event_id: event for event in self.events}
        for checkpoint in self.checkpoints:
            event = event_by_id.get(checkpoint.event_id)
            if (
                checkpoint.run_id != self.run.run_id
                or checkpoint.experiment_id != self.run.experiment_id
                or checkpoint.spec_hash != self.run.spec_hash
                or checkpoint.code_commit != self.run.code.commit
                or checkpoint.container_digest != self.run.container.image.content_hash
                or checkpoint.model_identity
                != f"{self.run.resolved_model.provider}:{self.run.resolved_model.model}"
                or checkpoint.integrity_hash != checkpoint_integrity_hash(checkpoint)
                or event is None
                or event.event_type != EventType.checkpoint_created
                or not isinstance(event.payload, CheckpointCreatedPayload)
                or event.payload.checkpoint_id != checkpoint.checkpoint_id
                or event.sequence != checkpoint.event_sequence
                or event.turn != checkpoint.turn
            ):
                raise ValueError("run journal contains an invalid checkpoint linkage")
        return self


class ExperimentJournal(DomainModel):
    """One atomically persisted experiment snapshot and transition ledger."""

    schema_version: Literal["caribou.experiment_journal.v1"] = (
        "caribou.experiment_journal.v1"
    )
    experiment: Experiment
    transitions: list[ExperimentTransitionRecord]

    @model_validator(mode="after")
    def validate_snapshot_and_transitions(self) -> "ExperimentJournal":
        validate_experiment_transition_pair(self.experiment, self.transitions)
        return self


def sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_json_bytes(model: BaseModel) -> bytes:
    """Return deterministic UTF-8 JSON for hashing and storage."""

    value = model.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def model_hash(model: BaseModel) -> str:
    return sha256_bytes(canonical_json_bytes(model))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # One lock inode per record directory, not one per record. This deliberately
    # trades some write concurrency for much lower metadata pressure on HPC
    # filesystems while still coordinating independent processes.
    lock_path = path.parent / ".caribou-domain.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_model(
    path: Path,
    model: BaseModel,
    *,
    expected_hash: Optional[str] = None,
) -> str:
    """Atomically write a model with optional compare-and-swap protection."""

    data = canonical_json_bytes(model)
    with _exclusive_lock(path):
        current_hash = file_hash(path) if path.exists() else None
        if expected_hash is not None and current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        _atomic_replace(path, data)
    return sha256_bytes(data)


def initialize_run_journal(path: Path, run: Run) -> str:
    """Create the durable journal for a run before its first transition."""

    if run.event_sequence != 0:
        raise IntegrityError("a new run journal must start at event sequence zero")
    data = canonical_json_bytes(RunJournal(run=run, events=[]))
    with _exclusive_lock(path):
        if path.exists():
            raise ConcurrentUpdateError(f"run journal already exists: {path}")
        _atomic_replace(path, data)
    return sha256_bytes(data)


def initialize_experiment_journal(path: Path, experiment: Experiment) -> str:
    if experiment.transition_sequence != 0:
        raise IntegrityError("a new experiment journal must start at sequence zero")
    data = canonical_json_bytes(
        ExperimentJournal(experiment=experiment, transitions=[])
    )
    with _exclusive_lock(path):
        if path.exists():
            raise ConcurrentUpdateError(f"experiment journal already exists: {path}")
        _atomic_replace(path, data)
    return sha256_bytes(data)


def commit_run_transition(
    path: Path,
    transition: RunTransition,
    *,
    expected_hash: str,
) -> str:
    """Atomically commit a state snapshot and its corresponding event.

    The journal is one replaceable record, so a crash cannot expose the new
    snapshot without the event or the event without the snapshot.
    """

    if not transition.applied or transition.event is None:
        raise PersistenceError("only an applied transition can be committed")
    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"run journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = RunJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(f"invalid run journal at {path}: {exc}") from exc
        validate_run_event_pair(current.run, current.events)
        event = transition.event
        if transition.run.run_id != current.run.run_id:
            raise IntegrityError("transition belongs to a different run attempt")
        if transition.run.event_sequence != current.run.event_sequence + 1:
            raise IntegrityError(
                "transition does not advance the snapshot by exactly one event"
            )
        if event.sequence != transition.run.event_sequence:
            raise IntegrityError("transition event and snapshot sequences disagree")
        if event.event_type != EventType.state_transition or not isinstance(
            event.payload, StateTransitionPayload
        ):
            raise IntegrityError("committed lifecycle event must be a state transition")
        if (
            event.payload.from_state != current.run.state
            or event.payload.to_state != transition.run.state
            or transition.run.state not in RUN_TRANSITIONS[current.run.state]
        ):
            raise IntegrityError(
                "transition event does not describe a legal snapshot change"
            )
        mutable_fields = {
            "state",
            "updated_at",
            "event_sequence",
            "queued_at",
            "started_at",
            "ended_at",
            "terminal_outcome",
            "end_reason",
            "exit_code",
            "checkpoint_ids",
            "resume_eligible",
        }
        previous_values = current.run.model_dump(mode="python")
        next_values = transition.run.model_dump(mode="python")
        for field in mutable_fields:
            previous_values.pop(field, None)
            next_values.pop(field, None)
        if previous_values != next_values:
            raise IntegrityError(
                "transition attempted to mutate immutable run configuration"
            )
        updated = RunJournal(
            run=transition.run,
            events=[*current.events, event],
            checkpoints=current.checkpoints,
        )
        validate_run_event_pair(updated.run, updated.events)
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def read_run_journal(path: Path) -> RunJournal:
    journal = read_model(path, RunJournal)
    validate_run_event_pair(journal.run, journal.events)
    return journal


def commit_run_event(
    path: Path,
    updated_run: Run,
    event: Event,
    *,
    expected_hash: str,
) -> str:
    """Atomically append one durable non-transition event and its run snapshot.

    This is the application-service counterpart to ``commit_run_transition``.
    It permits only event cursors, current execution position, and durable-record
    link fields to change; frozen scientific and execution configuration remains
    immutable.
    """

    if not event.durable or event.event_type == EventType.state_transition:
        raise PersistenceError(
            "commit_run_event requires a durable non-transition event"
        )
    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"run journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = RunJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(f"invalid run journal at {path}: {exc}") from exc
        if updated_run.run_id != current.run.run_id:
            raise IntegrityError("event update belongs to a different run attempt")
        if (
            updated_run.event_sequence != current.run.event_sequence + 1
            or event.sequence != updated_run.event_sequence
        ):
            raise IntegrityError("event update must advance the cursor exactly once")
        if (
            event.run_id != updated_run.run_id
            or event.experiment_id != updated_run.experiment_id
        ):
            raise IntegrityError("event update crosses a run or experiment boundary")
        if updated_run.updated_at != event.occurred_at:
            raise IntegrityError("run update timestamp must equal the event timestamp")
        if updated_run.current_turn != event.turn:
            raise IntegrityError("run current turn must equal the event turn")

        link_fields: dict[str, Optional[str]] = {
            "artifact_ids": None,
            "metric_record_ids": None,
            "failure_ids": None,
            "checkpoint_ids": None,
            "budget_record_ids": None,
        }
        if isinstance(event.payload, ArtifactCreatedPayload):
            link_fields["artifact_ids"] = event.payload.artifact_id
        elif isinstance(event.payload, MetricRecordedPayload):
            link_fields["metric_record_ids"] = event.payload.metric_record_id
        elif isinstance(event.payload, FailureRecordedPayload):
            link_fields["failure_ids"] = event.payload.failure_id
        elif isinstance(event.payload, CheckpointCreatedPayload):
            link_fields["checkpoint_ids"] = event.payload.checkpoint_id
        elif isinstance(event.payload, BudgetRecordedPayload):
            link_fields["budget_record_ids"] = event.payload.budget_record_id

        for field, expected_identifier in link_fields.items():
            before = list(getattr(current.run, field))
            after = list(getattr(updated_run, field))
            expected = (
                before
                if expected_identifier is None
                else [*before, expected_identifier]
            )
            if after != expected:
                raise IntegrityError(
                    f"event update has invalid {field} linkage for its payload"
                )

        mutable_fields = {
            "updated_at",
            "event_sequence",
            "current_turn",
            "current_agent",
            *link_fields,
        }
        previous_values = current.run.model_dump(mode="python")
        next_values = updated_run.model_dump(mode="python")
        for field in mutable_fields:
            previous_values.pop(field, None)
            next_values.pop(field, None)
        if previous_values != next_values:
            raise IntegrityError(
                "event update attempted to mutate frozen run configuration or state"
            )
        updated = RunJournal(
            run=updated_run,
            events=[*current.events, event],
            checkpoints=current.checkpoints,
        )
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def commit_run_checkpoint(
    path: Path,
    updated_run: Run,
    checkpoint: Checkpoint,
    event: Event,
    *,
    expected_hash: str,
) -> str:
    """Atomically publish one complete checkpoint and its durable event.

    Component artifacts are committed before this call. They do not constitute a
    resumable checkpoint until this single journal replacement links the validated
    envelope, event, and run cursor together.
    """

    if (
        not event.durable
        or event.event_type != EventType.checkpoint_created
        or not isinstance(event.payload, CheckpointCreatedPayload)
        or event.payload.checkpoint_id != checkpoint.checkpoint_id
    ):
        raise PersistenceError(
            "checkpoint commit requires a matching checkpoint_created event"
        )
    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"run journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = RunJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(f"invalid run journal at {path}: {exc}") from exc
        if current.run.state not in {RunState.running, RunState.checkpointed}:
            raise IntegrityError(
                "checkpoint can be committed only at an active boundary"
            )
        if checkpoint.checkpoint_id in current.run.checkpoint_ids:
            raise IntegrityError("checkpoint is already linked to the run")
        if (
            updated_run.run_id != current.run.run_id
            or checkpoint.run_id != current.run.run_id
            or checkpoint.experiment_id != current.run.experiment_id
            or event.run_id != current.run.run_id
            or event.experiment_id != current.run.experiment_id
        ):
            raise IntegrityError(
                "checkpoint commit crosses a run or experiment boundary"
            )
        if (
            updated_run.event_sequence != current.run.event_sequence + 1
            or event.sequence != updated_run.event_sequence
            or checkpoint.event_sequence != event.sequence
            or checkpoint.event_id != event.event_id
        ):
            raise IntegrityError(
                "checkpoint commit must advance the cursor exactly once"
            )
        if (
            checkpoint.turn != current.run.current_turn
            or event.turn != checkpoint.turn
            or updated_run.current_turn != checkpoint.turn
        ):
            raise IntegrityError("checkpoint must describe the completed current turn")
        if checkpoint.integrity_hash != checkpoint_integrity_hash(checkpoint):
            raise IntegrityError("checkpoint integrity hash is invalid")
        if (
            checkpoint.spec_hash != current.run.spec_hash
            or checkpoint.code_commit != current.run.code.commit
            or checkpoint.container_digest != current.run.container.image.content_hash
            or checkpoint.model_identity
            != f"{current.run.resolved_model.provider}:{current.run.resolved_model.model}"
        ):
            raise IntegrityError("checkpoint is incompatible with the frozen run")

        previous_values = current.run.model_dump(mode="python")
        next_values = updated_run.model_dump(mode="python")
        for field in {
            "updated_at",
            "event_sequence",
            "current_turn",
            "current_agent",
            "checkpoint_ids",
        }:
            previous_values.pop(field, None)
            next_values.pop(field, None)
        if previous_values != next_values:
            raise IntegrityError(
                "checkpoint commit attempted to mutate frozen run configuration or state"
            )
        if updated_run.checkpoint_ids != [
            *current.run.checkpoint_ids,
            checkpoint.checkpoint_id,
        ]:
            raise IntegrityError("checkpoint commit has invalid run linkage")
        if updated_run.updated_at != event.occurred_at:
            raise IntegrityError("checkpoint timestamp must equal its event timestamp")

        updated = RunJournal(
            run=updated_run,
            events=[*current.events, event],
            checkpoints=[*current.checkpoints, checkpoint],
        )
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def commit_run_scheduler_binding(
    path: Path,
    updated_run: Run,
    event: Event,
    *,
    expected_hash: str,
) -> str:
    """Atomically bind one scheduler job identity and append its durable event.

    Scheduler identity is assigned only after ``sbatch`` returns, so it cannot be
    part of the originally frozen run snapshot. This deliberately narrow commit
    permits exactly one ``scheduler_job_id`` assignment while the attempt remains
    queued; all scientific and other execution configuration stays immutable.
    """

    if (
        not event.durable
        or event.event_type != EventType.heartbeat
        or event.stage != "scheduler_submission"
    ):
        raise PersistenceError(
            "scheduler binding requires a durable scheduler_submission heartbeat"
        )
    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"run journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = RunJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(f"invalid run journal at {path}: {exc}") from exc
        if current.run.state.value not in {"queued", "cancelling"}:
            raise IntegrityError(
                "scheduler identity can be bound only while queued or cancelling"
            )
        if current.run.scheduler_job_id is not None:
            raise IntegrityError("scheduler identity is already bound")
        if not updated_run.scheduler_job_id:
            raise IntegrityError("scheduler binding requires a non-empty job ID")
        if updated_run.run_id != current.run.run_id:
            raise IntegrityError("scheduler binding belongs to a different run")
        if (
            updated_run.event_sequence != current.run.event_sequence + 1
            or event.sequence != updated_run.event_sequence
        ):
            raise IntegrityError("scheduler binding must advance the cursor once")
        if (
            event.run_id != updated_run.run_id
            or event.experiment_id != updated_run.experiment_id
        ):
            raise IntegrityError("scheduler binding crosses a run or experiment")
        if updated_run.updated_at != event.occurred_at:
            raise IntegrityError("scheduler binding timestamp differs from its event")
        if updated_run.current_turn != event.turn:
            raise IntegrityError("scheduler binding turn differs from its event")

        mutable_fields = {
            "scheduler_job_id",
            "updated_at",
            "event_sequence",
            "current_turn",
        }
        previous_values = current.run.model_dump(mode="python")
        next_values = updated_run.model_dump(mode="python")
        for field in mutable_fields:
            previous_values.pop(field, None)
            next_values.pop(field, None)
        if previous_values != next_values:
            raise IntegrityError(
                "scheduler binding attempted to mutate frozen run configuration"
            )
        updated = RunJournal(
            run=updated_run,
            events=[*current.events, event],
            checkpoints=current.checkpoints,
        )
        validate_run_event_pair(updated.run, updated.events)
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def commit_experiment_transition(
    path: Path,
    transition: ExperimentTransitionResult,
    *,
    expected_hash: str,
) -> str:
    """Atomically commit an experiment snapshot and transition record."""

    if not transition.applied or transition.record is None:
        raise PersistenceError("only an applied experiment transition can be committed")
    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"experiment journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = ExperimentJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(
                f"invalid experiment journal at {path}: {exc}"
            ) from exc
        record = transition.record
        if transition.experiment.experiment_id != current.experiment.experiment_id:
            raise IntegrityError("transition belongs to a different experiment")
        if (
            record.from_state != current.experiment.state
            or record.to_state != transition.experiment.state
            or record.to_state not in EXPERIMENT_TRANSITIONS[current.experiment.state]
        ):
            raise IntegrityError(
                "record does not describe a legal experiment transition"
            )
        if (
            transition.experiment.transition_sequence
            != current.experiment.transition_sequence + 1
        ):
            raise IntegrityError(
                "experiment transition sequence did not advance exactly once"
            )
        immutable_before = current.experiment.model_dump(mode="python")
        immutable_after = transition.experiment.model_dump(mode="python")
        for field in ("state", "updated_at", "completed_at", "transition_sequence"):
            immutable_before.pop(field, None)
            immutable_after.pop(field, None)
        if immutable_before != immutable_after:
            raise IntegrityError(
                "transition attempted to mutate immutable experiment fields"
            )
        updated = ExperimentJournal(
            experiment=transition.experiment,
            transitions=[*current.transitions, record],
        )
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def commit_experiment_run_link(
    path: Path,
    updated_experiment: Experiment,
    child_run: Run,
    *,
    expected_hash: str,
) -> str:
    """Atomically append one linked resume attempt to an active experiment."""

    with _exclusive_lock(path):
        if not path.exists():
            raise IntegrityError(f"experiment journal does not exist: {path}")
        current_hash = file_hash(path)
        if current_hash != expected_hash:
            raise ConcurrentUpdateError(
                f"compare-and-swap conflict for {path}: expected {expected_hash}, "
                f"found {current_hash}"
            )
        try:
            current = ExperimentJournal.model_validate_json(path.read_bytes())
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(
                f"invalid experiment journal at {path}: {exc}"
            ) from exc
        if current.experiment.state != ExperimentState.active:
            raise IntegrityError(
                "resume attempts can be linked only to an active experiment"
            )
        if (
            updated_experiment.experiment_id != current.experiment.experiment_id
            or child_run.experiment_id != current.experiment.experiment_id
        ):
            raise IntegrityError("resume attempt belongs to another experiment")
        if (
            child_run.resumed_from_run_id is None
            or child_run.resume_checkpoint_id is None
            or child_run.resumed_from_run_id not in current.experiment.run_ids
        ):
            raise IntegrityError("resume attempt has invalid source lineage")
        if updated_experiment.run_ids != [
            *current.experiment.run_ids,
            child_run.run_id,
        ]:
            raise IntegrityError("experiment must append exactly one resume attempt")
        if child_run.run_id in current.experiment.run_ids:
            raise IntegrityError("resume attempt is already linked")
        before = current.experiment.model_dump(mode="python")
        after = updated_experiment.model_dump(mode="python")
        for field in {"run_ids", "updated_at"}:
            before.pop(field, None)
            after.pop(field, None)
        if before != after:
            raise IntegrityError("run linkage mutated frozen experiment fields")
        if updated_experiment.updated_at < current.experiment.updated_at:
            raise IntegrityError("experiment linkage timestamp moved backward")
        updated = ExperimentJournal(
            experiment=updated_experiment,
            transitions=current.transitions,
        )
        data = canonical_json_bytes(updated)
        _atomic_replace(path, data)
    return sha256_bytes(data)


def read_experiment_journal(path: Path) -> ExperimentJournal:
    journal = read_model(path, ExperimentJournal)
    validate_experiment_transition_pair(journal.experiment, journal.transitions)
    return journal


def read_model(path: Path, model_type: Type[ModelT]) -> ModelT:
    """Read and strictly validate a stored model."""

    try:
        return model_type.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise IntegrityError(
            f"invalid {model_type.__name__} record at {path}: {exc}"
        ) from exc


def append_event(path: Path, event: Event) -> str:
    """Append one durable event by atomically replacing its JSONL stream."""

    if not event.durable:
        raise PersistenceError(
            "ephemeral events cannot be appended to the durable ledger"
        )
    with _exclusive_lock(path):
        events = _read_events_unlocked(path)
        validate_event_stream([*events, event])
        lines = [canonical_json_bytes(item) for item in (*events, event)]
        data = b"\n".join(lines) + b"\n"
        _atomic_replace(path, data)
    return sha256_bytes(data)


def _read_events_unlocked(path: Path) -> list[Event]:
    if not path.exists():
        return []
    events: list[Event] = []
    for number, line in enumerate(path.read_bytes().splitlines(), start=1):
        if not line.strip():
            raise IntegrityError(f"blank line in event stream {path} at line {number}")
        try:
            events.append(Event.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise IntegrityError(
                f"invalid event stream {path} at line {number}: {exc}"
            ) from exc
    validate_event_stream(events)
    return events


def read_events(path: Path) -> list[Event]:
    with _exclusive_lock(path):
        return _read_events_unlocked(path)


def validate_event_stream(events: Sequence[Event]) -> None:
    """Reject gaps, duplicates, mixed attempts, and backward timestamps."""

    if not events:
        return
    experiment_id = events[0].experiment_id
    run_id = events[0].run_id
    seen_ids: set[str] = set()
    previous_time = events[0].occurred_at
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence:
            raise IntegrityError(
                f"event sequence gap: expected {expected_sequence}, found {event.sequence}"
            )
        if event.experiment_id != experiment_id or event.run_id != run_id:
            raise IntegrityError("event stream mixes experiments or run attempts")
        if event.event_id in seen_ids:
            raise IntegrityError(f"duplicate event ID {event.event_id}")
        if event.occurred_at < previous_time:
            raise IntegrityError("event timestamps move backward")
        seen_ids.add(event.event_id)
        previous_time = event.occurred_at


def validate_run_event_pair(run: Run, events: Sequence[Event]) -> None:
    """Verify that a run snapshot and its event ledger agree."""

    validate_event_stream(events)
    if run.event_sequence != len(events):
        raise IntegrityError(
            f"run event_sequence={run.event_sequence} but ledger has {len(events)} events"
        )
    if events and (
        events[-1].run_id != run.run_id or events[-1].experiment_id != run.experiment_id
    ):
        raise IntegrityError("run snapshot does not own the supplied event ledger")
    reconstructed_state = run.initial_state
    for event in events:
        if event.event_type != EventType.state_transition:
            continue
        if not isinstance(event.payload, StateTransitionPayload):
            raise IntegrityError("state transition has an invalid payload")
        if event.payload.from_state != reconstructed_state:
            raise IntegrityError(
                "state-transition events do not form a continuous chain"
            )
        if event.payload.to_state not in RUN_TRANSITIONS[reconstructed_state]:
            raise IntegrityError("event ledger contains an illegal state transition")
        reconstructed_state = event.payload.to_state
    if reconstructed_state != run.state:
        raise IntegrityError("run state cannot be reconstructed from its event ledger")


def validate_experiment_transition_pair(
    experiment: Experiment,
    transitions: Sequence[ExperimentTransitionRecord],
) -> None:
    if experiment.transition_sequence != len(transitions):
        raise IntegrityError(
            "experiment transition_sequence does not match its transition ledger"
        )
    state = ExperimentState.draft
    previous_time = experiment.created_at
    for sequence, record in enumerate(transitions, start=1):
        if (
            record.experiment_id != experiment.experiment_id
            or record.sequence != sequence
        ):
            raise IntegrityError(
                "experiment transition ledger has mixed IDs or a sequence gap"
            )
        if record.occurred_at < previous_time:
            raise IntegrityError("experiment transition timestamps move backward")
        if (
            record.from_state != state
            or record.to_state not in EXPERIMENT_TRANSITIONS[state]
        ):
            raise IntegrityError(
                "experiment transition ledger contains an illegal state edge"
            )
        state = record.to_state
        previous_time = record.occurred_at
    if state != experiment.state:
        raise IntegrityError("experiment state cannot be reconstructed from its ledger")


def verify_artifact(
    path: Path,
    expected_hash: str,
    expected_size: Optional[int] = None,
    *,
    root: Optional[Path] = None,
) -> None:
    """Verify an artifact without modifying it."""

    if path.is_symlink():
        raise IntegrityError(f"artifact symlinks are not accepted: {path}")
    if not path.is_file():
        raise IntegrityError(f"artifact is missing or not a file: {path}")
    resolved = path.resolve(strict=True)
    if root is not None and not resolved.is_relative_to(root.resolve(strict=True)):
        raise IntegrityError(f"artifact escapes its declared storage root: {path}")
    actual_size = path.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise IntegrityError(
            f"artifact size mismatch for {path}: expected {expected_size}, found {actual_size}"
        )
    actual_hash = file_hash(path)
    if actual_hash != expected_hash:
        raise IntegrityError(
            f"artifact hash mismatch for {path}: expected {expected_hash}, found {actual_hash}"
        )
