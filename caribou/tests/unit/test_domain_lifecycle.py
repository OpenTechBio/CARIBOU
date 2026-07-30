"""State-machine tests for immutable CARIBOU run attempts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from caribou.domain.enums import CheckpointComponent, ExperimentState, RunState
from caribou.domain.lifecycle import (
    RUN_TRANSITIONS,
    LifecycleError,
    create_resume_attempt,
    transition_experiment,
    transition_run,
)
from caribou.domain.models import Checkpoint, Experiment, checkpoint_integrity_hash
from caribou.domain.enums import RunOutcome

from .test_domain_models import (
    ARTIFACT_ID,
    CHECKPOINT_ID,
    EXP_ID,
    HASH_A,
    make_run,
)


def make_checkpoint(run, **updates):
    values = {
        "checkpoint_id": CHECKPOINT_ID,
        "experiment_id": run.experiment_id,
        "run_id": run.run_id,
        "event_id": "evt_" + "a" * 32,
        "event_sequence": run.event_sequence,
        "stage": "analysis",
        "turn": run.current_turn,
        "components": [CheckpointComponent.artifact_manifest],
        "artifact_manifest_id": ARTIFACT_ID,
        "spec_hash": run.spec_hash,
        "code_commit": run.code.commit,
        "container_digest": run.container.image.content_hash,
        "model_identity": f"{run.resolved_model.provider}:{run.resolved_model.model}",
        "integrity_hash": HASH_A,
    }
    values.update(updates)
    provisional = Checkpoint.model_validate(values)
    return Checkpoint.model_validate_json(
        provisional.model_copy(
            update={"integrity_hash": checkpoint_integrity_hash(provisional)}
        ).model_dump_json()
    )


def advance_to_running():
    run = make_run()
    for target in (
        RunState.validated,
        RunState.planned,
        RunState.starting,
        RunState.running,
    ):
        result = transition_run(
            run, target, reason=f"enter {target.value}", actor="test"
        )
        assert result.applied and result.event is not None
        assert result.event.sequence == run.event_sequence + 1
        run = result.run
    return run


def make_run_in_state(state: RunState):
    now = datetime.now(timezone.utc)
    updates = {"state": state, "created_at": now, "updated_at": now}
    if state in {
        RunState.queued,
        RunState.starting,
        RunState.running,
        RunState.checkpointed,
    }:
        updates["queued_at"] = now
    if state in {
        RunState.running,
        RunState.checkpointed,
        RunState.cancelling,
        RunState.succeeded,
    }:
        updates["started_at"] = now
    if state == RunState.checkpointed:
        updates["checkpoint_ids"] = [CHECKPOINT_ID]
    outcomes = {
        RunState.succeeded: RunOutcome.succeeded,
        RunState.failed: RunOutcome.failed,
        RunState.cancelled: RunOutcome.cancelled,
        RunState.rejected: RunOutcome.rejected,
        RunState.resumable: RunOutcome.interrupted_resumable,
    }
    if state in outcomes:
        updates.update(
            ended_at=now,
            terminal_outcome=outcomes[state],
            end_reason="fixture terminal state",
        )
    if state == RunState.resumable:
        updates["checkpoint_ids"] = [CHECKPOINT_ID]
        updates["resume_eligible"] = True
    return make_run(**updates)


def test_legal_transitions_emit_exactly_one_ordered_event() -> None:
    run = advance_to_running()
    assert run.state == RunState.running
    assert run.event_sequence == 4
    assert run.started_at is not None


def test_transition_matrix_is_exhaustive_for_allowed_and_forbidden_edges() -> None:
    all_states = set(RunState)
    for source, allowed_targets in RUN_TRANSITIONS.items():
        for target in allowed_targets:
            source_run = make_run_in_state(source)
            checkpoint = (
                make_checkpoint(source_run)
                if target in {RunState.checkpointed, RunState.resumable}
                else None
            )
            result = transition_run(
                source_run,
                target,
                reason="matrix test",
                actor="test",
                checkpoint=checkpoint,
            )
            assert result.run.state == target
        for target in all_states - set(allowed_targets) - {source}:
            with pytest.raises(LifecycleError):
                transition_run(
                    make_run_in_state(source),
                    target,
                    reason="forbidden matrix edge",
                    actor="test",
                    checkpoint=make_checkpoint(make_run_in_state(source)),
                )


def test_same_state_is_idempotent_and_illegal_transition_is_rejected() -> None:
    run = make_run()
    repeated = transition_run(run, RunState.draft, reason="duplicate", actor="test")
    assert repeated.run is run
    assert repeated.event is None
    assert not repeated.applied
    with pytest.raises(LifecycleError, match="illegal"):
        transition_run(run, RunState.succeeded, reason="skip", actor="test")


def test_checkpoint_is_required_for_checkpointed_and_resumable_states() -> None:
    running = advance_to_running()
    with pytest.raises(LifecycleError, match="checkpoint"):
        transition_run(running, RunState.checkpointed, reason="save", actor="test")
    checkpointed = transition_run(
        running,
        RunState.checkpointed,
        reason="save",
        actor="test",
        checkpoint=make_checkpoint(running),
    ).run
    interrupted = transition_run(
        checkpointed,
        RunState.resumable,
        reason="wall time",
        actor="scheduler",
        checkpoint=make_checkpoint(checkpointed),
        exit_code=124,
    ).run
    assert interrupted.state == RunState.resumable
    assert interrupted.resume_eligible


def test_resumption_creates_a_new_linked_attempt_and_never_reopens_old_attempt() -> (
    None
):
    running = advance_to_running()
    interrupted = transition_run(
        running,
        RunState.resumable,
        reason="preempted",
        actor="scheduler",
        checkpoint=make_checkpoint(running),
    ).run
    checkpoint = make_checkpoint(running)
    resumed = create_resume_attempt(
        interrupted,
        checkpoint=checkpoint,
        idempotency_key="exp/condition/0/2",
    )
    assert resumed.run_id != interrupted.run_id
    assert resumed.attempt_index == interrupted.attempt_index + 1
    assert resumed.state == RunState.planned
    assert resumed.resumed_from_run_id == interrupted.run_id
    assert resumed.resume_checkpoint_id == CHECKPOINT_ID
    assert interrupted.state == RunState.resumable
    with pytest.raises(LifecycleError):
        create_resume_attempt(make_run(), checkpoint=checkpoint, idempotency_key="bad")
    incompatible = make_checkpoint(running, code_commit="d" * 40)
    with pytest.raises(LifecycleError, match="code commit changed"):
        create_resume_attempt(
            interrupted,
            checkpoint=incompatible,
            idempotency_key="exp/condition/0/incompatible",
        )


def test_failed_attempt_is_terminal_and_not_resumable() -> None:
    running = advance_to_running()
    failed = transition_run(
        running, RunState.failed, reason="executor failed", actor="runner"
    ).run
    with pytest.raises(LifecycleError, match="illegal"):
        transition_run(failed, RunState.queued, reason="retry", actor="runner")
    with pytest.raises(LifecycleError, match="resumable"):
        create_resume_attempt(
            failed,
            checkpoint=make_checkpoint(failed),
            idempotency_key="retry",
        )


def test_backward_timestamp_is_rejected() -> None:
    run = make_run()
    with pytest.raises(LifecycleError):
        transition_run(
            run,
            RunState.validated,
            reason="clock moved",
            actor="test",
            at=run.created_at - timedelta(seconds=1),
        )


def test_experiment_transitions_are_terminal_and_idempotent() -> None:
    experiment = Experiment(
        experiment_id=EXP_ID,
        spec_id="spec_" + "8" * 32,
        spec_version=1,
        spec_hash=HASH_A,
        owner="researcher",
    )
    repeated = transition_experiment(
        experiment,
        ExperimentState.draft,
        reason="duplicate",
        actor="test",
    )
    assert repeated.experiment is experiment
    assert not repeated.applied
    for state in (
        ExperimentState.validated,
        ExperimentState.planned,
        ExperimentState.active,
        ExperimentState.aggregating,
        ExperimentState.completed,
    ):
        result = transition_experiment(
            experiment,
            state,
            reason=f"enter {state.value}",
            actor="test",
        )
        assert result.record is not None
        assert result.record.sequence == result.experiment.transition_sequence
        experiment = result.experiment
    assert experiment.completed_at is not None
    with pytest.raises(LifecycleError):
        transition_experiment(
            experiment,
            ExperimentState.active,
            reason="illegal reopen",
            actor="test",
        )
