"""Explicit state machines for immutable experiment and run records.

A terminal attempt is never reopened.  In particular, ``failed`` is terminal.
Only an attempt deliberately terminated as ``resumable`` with a complete
checkpoint can be used to construct a new, linked attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, Optional

from .enums import EventType, ExperimentState, InterfaceOrigin, RunOutcome, RunState
from .ids import new_id
from .models import (
    Checkpoint,
    Event,
    Experiment,
    ExperimentTransitionRecord,
    NonEmptyStr,
    Run,
    StateTransitionPayload,
    checkpoint_integrity_hash,
    utc_now,
)


RUN_TRANSITIONS: Dict[RunState, FrozenSet[RunState]] = {
    RunState.draft: frozenset(
        {RunState.validated, RunState.rejected, RunState.cancelled}
    ),
    RunState.validated: frozenset(
        {RunState.planned, RunState.rejected, RunState.cancelled}
    ),
    RunState.planned: frozenset(
        {RunState.queued, RunState.starting, RunState.rejected, RunState.cancelled}
    ),
    RunState.queued: frozenset(
        {
            RunState.starting,
            RunState.cancelling,
            RunState.cancelled,
            RunState.failed,
            RunState.resumable,
        }
    ),
    RunState.starting: frozenset(
        {RunState.running, RunState.cancelling, RunState.failed, RunState.resumable}
    ),
    RunState.running: frozenset(
        {
            RunState.checkpointed,
            RunState.cancelling,
            RunState.succeeded,
            RunState.failed,
            RunState.resumable,
        }
    ),
    RunState.checkpointed: frozenset(
        {
            RunState.running,
            RunState.cancelling,
            RunState.succeeded,
            RunState.failed,
            RunState.resumable,
        }
    ),
    RunState.cancelling: frozenset(
        {RunState.cancelled, RunState.failed, RunState.resumable}
    ),
    RunState.cancelled: frozenset(),
    RunState.failed: frozenset(),
    RunState.resumable: frozenset(),
    RunState.succeeded: frozenset(),
    RunState.rejected: frozenset(),
}

EXPERIMENT_TRANSITIONS: Dict[ExperimentState, FrozenSet[ExperimentState]] = {
    ExperimentState.draft: frozenset(
        {ExperimentState.validated, ExperimentState.rejected, ExperimentState.cancelled}
    ),
    ExperimentState.validated: frozenset(
        {ExperimentState.planned, ExperimentState.rejected, ExperimentState.cancelled}
    ),
    ExperimentState.planned: frozenset(
        {ExperimentState.active, ExperimentState.rejected, ExperimentState.cancelled}
    ),
    ExperimentState.active: frozenset(
        {ExperimentState.aggregating, ExperimentState.failed, ExperimentState.cancelled}
    ),
    ExperimentState.aggregating: frozenset(
        {ExperimentState.completed, ExperimentState.failed, ExperimentState.cancelled}
    ),
    ExperimentState.completed: frozenset(),
    ExperimentState.cancelled: frozenset(),
    ExperimentState.failed: frozenset(),
    ExperimentState.rejected: frozenset(),
}

_OUTCOMES = {
    RunState.succeeded: RunOutcome.succeeded,
    RunState.failed: RunOutcome.failed,
    RunState.cancelled: RunOutcome.cancelled,
    RunState.rejected: RunOutcome.rejected,
    RunState.resumable: RunOutcome.interrupted_resumable,
}
_RUN_TERMINAL = frozenset(_OUTCOMES)
_EXPERIMENT_TERMINAL = frozenset(
    {
        ExperimentState.completed,
        ExperimentState.cancelled,
        ExperimentState.failed,
        ExperimentState.rejected,
    }
)


def _checkpoint_incompatibilities(run: Run, checkpoint: Checkpoint) -> list[str]:
    incompatibilities = []
    if checkpoint.status.value != "complete":
        incompatibilities.append("checkpoint is not complete")
    if checkpoint.integrity_hash != checkpoint_integrity_hash(checkpoint):
        incompatibilities.append("checkpoint integrity hash is invalid")
    if checkpoint.experiment_id != run.experiment_id or checkpoint.run_id != run.run_id:
        incompatibilities.append("checkpoint belongs to another experiment or attempt")
    if checkpoint.spec_hash != run.spec_hash:
        incompatibilities.append("experiment specification hash changed")
    if checkpoint.code_commit != run.code.commit:
        incompatibilities.append("code commit changed")
    if checkpoint.container_digest != run.container.image.content_hash:
        incompatibilities.append("container digest changed")
    model_identity = f"{run.resolved_model.provider}:{run.resolved_model.model}"
    if checkpoint.model_identity != model_identity:
        incompatibilities.append("model identity changed")
    if checkpoint.event_sequence > run.event_sequence:
        incompatibilities.append("checkpoint action cursor is ahead of the run")
    return incompatibilities


class LifecycleError(ValueError):
    """Raised when a requested lifecycle operation violates the state machine."""


@dataclass(frozen=True)
class RunTransition:
    run: Run
    event: Optional[Event]
    applied: bool


@dataclass(frozen=True)
class ExperimentTransitionResult:
    experiment: Experiment
    record: Optional[ExperimentTransitionRecord]
    applied: bool


def _validated_copy(record: object, updates: dict) -> object:
    candidate = record.model_copy(update=updates)  # type: ignore[attr-defined]
    return type(record).model_validate_json(  # type: ignore[attr-defined]
        candidate.model_dump_json()
    )


def transition_run(
    run: Run,
    target: RunState,
    *,
    reason: NonEmptyStr,
    actor: NonEmptyStr,
    at: Optional[datetime] = None,
    checkpoint: Optional[Checkpoint] = None,
    exit_code: Optional[int] = None,
) -> RunTransition:
    """Apply one legal transition and create its durable, ordered event.

    A repeated request for the current state is idempotent and emits no event.
    Persistence of the returned state/event pair is handled transactionally by
    the domain store; this function performs no I/O.
    """

    if target == run.state:
        return RunTransition(run=run, event=None, applied=False)
    if target not in RUN_TRANSITIONS[run.state]:
        raise LifecycleError(
            f"illegal run transition: {run.state.value} -> {target.value}"
        )
    timestamp = at or utc_now()
    updates = {
        "state": target,
        "updated_at": timestamp,
        "event_sequence": run.event_sequence + 1,
    }
    if target == RunState.queued and run.queued_at is None:
        updates["queued_at"] = timestamp
    if target == RunState.running and run.started_at is None:
        updates["started_at"] = timestamp
    if target == RunState.resumable:
        if checkpoint is None:
            raise LifecycleError("resumable transition requires a complete checkpoint")
        incompatibilities = _checkpoint_incompatibilities(run, checkpoint)
        if incompatibilities:
            raise LifecycleError(
                "incompatible transition checkpoint: " + "; ".join(incompatibilities)
            )
        checkpoint_id = checkpoint.checkpoint_id
        updates["checkpoint_ids"] = list(
            dict.fromkeys([*run.checkpoint_ids, checkpoint_id])
        )
        updates["resume_eligible"] = True
    elif target == RunState.checkpointed:
        if checkpoint is None:
            raise LifecycleError(
                "checkpointed transition requires a complete checkpoint"
            )
        incompatibilities = _checkpoint_incompatibilities(run, checkpoint)
        if incompatibilities:
            raise LifecycleError(
                "incompatible transition checkpoint: " + "; ".join(incompatibilities)
            )
        checkpoint_id = checkpoint.checkpoint_id
        updates["checkpoint_ids"] = list(
            dict.fromkeys([*run.checkpoint_ids, checkpoint_id])
        )
    if target in _RUN_TERMINAL:
        updates.update(
            {
                "ended_at": timestamp,
                "terminal_outcome": _OUTCOMES[target],
                "end_reason": reason,
                "exit_code": exit_code,
            }
        )
    try:
        updated = _validated_copy(run, updates)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc
    assert isinstance(updated, Run)
    event = Event(
        experiment_id=updated.experiment_id,
        run_id=updated.run_id,
        sequence=updated.event_sequence,
        occurred_at=timestamp,
        event_type=EventType.state_transition,
        turn=updated.current_turn,
        actor=actor,
        payload=StateTransitionPayload(
            from_state=run.state,
            to_state=target,
            reason=reason,
        ),
    )
    return RunTransition(run=updated, event=event, applied=True)


def transition_experiment(
    experiment: Experiment,
    target: ExperimentState,
    *,
    reason: NonEmptyStr,
    actor: NonEmptyStr,
    at: Optional[datetime] = None,
) -> ExperimentTransitionResult:
    """Apply one legal experiment transition, idempotently."""

    if target == experiment.state:
        return ExperimentTransitionResult(
            experiment=experiment,
            record=None,
            applied=False,
        )
    if target not in EXPERIMENT_TRANSITIONS[experiment.state]:
        raise LifecycleError(
            f"illegal experiment transition: {experiment.state.value} -> {target.value}"
        )
    timestamp = at or utc_now()
    updates = {
        "state": target,
        "updated_at": timestamp,
        "transition_sequence": experiment.transition_sequence + 1,
    }
    if target in _EXPERIMENT_TERMINAL:
        updates["completed_at"] = timestamp
    try:
        updated = _validated_copy(experiment, updates)
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc
    assert isinstance(updated, Experiment)
    record = ExperimentTransitionRecord(
        experiment_id=updated.experiment_id,
        sequence=updated.transition_sequence,
        occurred_at=timestamp,
        from_state=experiment.state,
        to_state=target,
        reason=reason,
        actor=actor,
    )
    return ExperimentTransitionResult(
        experiment=updated,
        record=record,
        applied=True,
    )


def create_resume_attempt(
    interrupted: Run,
    *,
    checkpoint: Checkpoint,
    idempotency_key: NonEmptyStr,
    interface: Optional[InterfaceOrigin] = None,
    at: Optional[datetime] = None,
) -> Run:
    """Construct a new planned attempt from a terminal resumable attempt."""

    if interrupted.state != RunState.resumable or not interrupted.resume_eligible:
        raise LifecycleError("only a resumable terminal attempt can be resumed")
    checkpoint_id = checkpoint.checkpoint_id
    if checkpoint_id not in interrupted.checkpoint_ids:
        raise LifecycleError(
            "resume checkpoint is not attached to the interrupted attempt"
        )
    incompatibilities = _checkpoint_incompatibilities(interrupted, checkpoint)
    if incompatibilities:
        raise LifecycleError(
            "incompatible resume checkpoint: " + "; ".join(incompatibilities)
        )
    timestamp = at or utc_now()
    updates = {
        "run_id": new_id("run"),
        "attempt_index": interrupted.attempt_index + 1,
        "idempotency_key": idempotency_key,
        "interface": interface or interrupted.interface,
        "state": RunState.planned,
        "initial_state": RunState.planned,
        "scheduler_job_id": None,
        "resumed_from_run_id": interrupted.run_id,
        "resume_checkpoint_id": checkpoint_id,
        "event_sequence": 0,
        "artifact_ids": [],
        "metric_record_ids": [],
        "failure_ids": [],
        "checkpoint_ids": [],
        "budget_record_ids": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "queued_at": None,
        "started_at": None,
        "ended_at": None,
        "terminal_outcome": None,
        "end_reason": None,
        "exit_code": None,
        "resume_eligible": False,
    }
    try:
        candidate = interrupted.model_copy(update=updates)
        return Run.model_validate_json(candidate.model_dump_json())
    except ValueError as exc:
        raise LifecycleError(str(exc)) from exc
