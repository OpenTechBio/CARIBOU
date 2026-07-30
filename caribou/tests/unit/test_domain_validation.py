"""Cross-record provenance graph validation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from caribou.domain.enums import (
    ArtifactType,
    CheckpointComponent,
    EventType,
    FailureCategory,
    FailureDisposition,
)
from caribou.domain.models import (
    Artifact,
    ArtifactCreatedPayload,
    Checkpoint,
    CheckpointCreatedPayload,
    Event,
    Experiment,
    FailureRecord,
    FailureRecordedPayload,
    HeartbeatPayload,
    ModelSpec,
    checkpoint_integrity_hash,
)
from caribou.domain.serialization import model_hash
from caribou.domain.validation import GraphIntegrityError, validate_record_graph

from .test_domain_models import (
    ARTIFACT_ID,
    CHECKPOINT_ID,
    COMMIT,
    EVENT_ID,
    EXP_ID,
    FAILURE_ID,
    HASH_A,
    make_run,
    make_spec,
)


PARENT_ARTIFACT_ID = "art_" + "7" * 32
PARENT_CHECKPOINT_ID = "chk_" + "8" * 32
SECOND_EVENT_ID = "evt_" + "9" * 32
THIRD_EVENT_ID = "evt_" + "a" * 32
SECOND_FAILURE_ID = "fail_" + "b" * 32


def valid_graph():
    spec = make_spec()
    spec_hash = model_hash(spec)
    run = make_run(
        condition_id="single",
        spec_hash=spec_hash,
        code=spec.code,
        resolved_model=spec.conditions[0].model,
        resolved_blueprint=spec.conditions[0].blueprint,
        resolved_prompt=spec.conditions[0].prompt,
        resolved_memory=spec.conditions[0].memory,
        resolved_inputs=list(spec.inputs),
        resolved_stop_rules=spec.stop_rules,
        resolved_budget=spec.budget,
        resources=spec.execution.resources,
        container=spec.execution.container,
        executor=spec.execution.executor,
    )
    experiment = Experiment(
        experiment_id=EXP_ID,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
        spec_hash=spec_hash,
        owner=spec.owner,
        run_ids=[run.run_id],
    )
    return spec, experiment, run


def test_complete_minimal_record_graph_is_consistent() -> None:
    spec, experiment, run = valid_graph()
    validate_record_graph(spec=spec, experiment=experiment, runs=[run])


@pytest.mark.parametrize(
    "change, message",
    [
        ({"condition_id": "unknown"}, "unknown condition"),
        ({"replicate_index": 99}, "replicate index"),
        (
            {"resolved_model": ModelSpec(provider="ollama", model="other")},
            "model drifted",
        ),
    ],
)
def test_cross_record_configuration_drift_fails_closed(change, message) -> None:
    spec, experiment, run = valid_graph()
    changed = run.model_copy(update=change)
    with pytest.raises(GraphIntegrityError, match=message):
        validate_record_graph(spec=spec, experiment=experiment, runs=[changed])


def test_missing_or_cross_experiment_run_links_fail_closed() -> None:
    spec, experiment, run = valid_graph()
    with pytest.raises(GraphIntegrityError, match="run_ids"):
        validate_record_graph(spec=spec, experiment=experiment, runs=[])
    changed = run.model_copy(update={"experiment_id": "exp_" + "9" * 32})
    with pytest.raises(GraphIntegrityError, match="another experiment"):
        validate_record_graph(spec=spec, experiment=experiment, runs=[changed])


def test_spec_mutation_invalidates_existing_experiment_hash() -> None:
    spec, experiment, run = valid_graph()
    changed_spec = spec.model_copy(
        update={"question": "A changed post-freeze question"}
    )
    with pytest.raises(GraphIntegrityError, match="canonical specification bytes"):
        validate_record_graph(spec=changed_spec, experiment=experiment, runs=[run])


def test_artifact_requires_matching_artifact_created_producer_event() -> None:
    spec, experiment, run = valid_graph()
    event = Event(
        event_id=EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=1,
        event_type=EventType.heartbeat,
        actor="runner",
        payload=HeartbeatPayload(message="not an artifact event"),
    )
    artifact = Artifact(
        artifact_id=ARTIFACT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        producer_event_id=event.event_id,
        producer="runner",
        artifact_type=ArtifactType.log,
        role="stdout",
        filename="stdout.txt",
        storage_uri="artifacts/stdout.txt",
        content_hash=HASH_A,
        media_type="text/plain",
        size_bytes=1,
        owner="researcher",
    )
    run = run.model_copy(
        update={"event_sequence": 1, "artifact_ids": (artifact.artifact_id,)}
    )
    with pytest.raises(GraphIntegrityError, match="invalid producer event"):
        validate_record_graph(
            spec=spec,
            experiment=experiment,
            runs=[run],
            events=[event],
            artifacts=[artifact],
        )


def test_failure_requires_matching_failure_recorded_event() -> None:
    spec, experiment, run = valid_graph()
    event = Event(
        event_id=EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=1,
        event_type=EventType.heartbeat,
        actor="runner",
        payload=HeartbeatPayload(message="not a failure event"),
    )
    failure = FailureRecord(
        failure_id=FAILURE_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        event_id=event.event_id,
        category=FailureCategory.execution,
        stage="analysis",
        code="failure",
        message="failure",
        fatal=False,
        retryable=False,
        attempt=run.attempt_index,
        detected_by="runner",
        downstream_effect="none",
        disposition=FailureDisposition.investigate,
    )
    run = run.model_copy(
        update={"event_sequence": 1, "failure_ids": (failure.failure_id,)}
    )
    with pytest.raises(GraphIntegrityError, match="invalid recorded event"):
        validate_record_graph(
            spec=spec,
            experiment=experiment,
            runs=[run],
            events=[event],
            failures=[failure],
        )


def test_artifact_parent_must_precede_child_producer_event() -> None:
    spec, experiment, run = valid_graph()
    start = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)
    child_event = Event(
        event_id=EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=1,
        occurred_at=start,
        event_type=EventType.artifact_created,
        actor="runner",
        payload=ArtifactCreatedPayload(artifact_id=ARTIFACT_ID),
    )
    parent_event = Event(
        event_id=SECOND_EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=2,
        occurred_at=start + timedelta(seconds=1),
        event_type=EventType.artifact_created,
        actor="runner",
        payload=ArtifactCreatedPayload(artifact_id=PARENT_ARTIFACT_ID),
    )
    child = Artifact(
        artifact_id=ARTIFACT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        producer_event_id=child_event.event_id,
        producer="runner",
        artifact_type=ArtifactType.log,
        role="child",
        filename="child.txt",
        storage_uri="artifacts/child.txt",
        content_hash=HASH_A,
        media_type="text/plain",
        size_bytes=1,
        parent_artifact_ids=[PARENT_ARTIFACT_ID],
        owner=run.owner,
    )
    parent = child.model_copy(
        update={
            "artifact_id": PARENT_ARTIFACT_ID,
            "producer_event_id": parent_event.event_id,
            "role": "parent",
            "filename": "parent.txt",
            "storage_uri": "artifacts/parent.txt",
            "parent_artifact_ids": (),
        }
    )
    run = run.model_copy(
        update={
            "event_sequence": 2,
            "artifact_ids": (child.artifact_id, parent.artifact_id),
        }
    )
    with pytest.raises(GraphIntegrityError, match="parent does not precede"):
        validate_record_graph(
            spec=spec,
            experiment=experiment,
            runs=[run],
            events=[child_event, parent_event],
            artifacts=[child, parent],
        )


def test_failure_cause_must_precede_effect_event() -> None:
    spec, experiment, run = valid_graph()
    child_event = Event(
        event_id=EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=1,
        event_type=EventType.failure_recorded,
        actor="runner",
        payload=FailureRecordedPayload(failure_id=FAILURE_ID),
    )
    cause_event = Event(
        event_id=SECOND_EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=2,
        event_type=EventType.failure_recorded,
        actor="runner",
        payload=FailureRecordedPayload(failure_id=SECOND_FAILURE_ID),
    )
    common = {
        "experiment_id": experiment.experiment_id,
        "run_id": run.run_id,
        "category": FailureCategory.execution,
        "stage": "analysis",
        "code": "failure",
        "message": "failure",
        "fatal": False,
        "retryable": False,
        "attempt": run.attempt_index,
        "detected_by": "runner",
        "downstream_effect": "none",
        "disposition": FailureDisposition.investigate,
    }
    child = FailureRecord(
        failure_id=FAILURE_ID,
        event_id=child_event.event_id,
        caused_by_failure_id=SECOND_FAILURE_ID,
        **common,
    )
    cause = FailureRecord(
        failure_id=SECOND_FAILURE_ID,
        event_id=cause_event.event_id,
        **common,
    )
    run = run.model_copy(
        update={
            "event_sequence": 2,
            "failure_ids": (child.failure_id, cause.failure_id),
        }
    )
    with pytest.raises(GraphIntegrityError, match="invalid causal failure"):
        validate_record_graph(
            spec=spec,
            experiment=experiment,
            runs=[run],
            events=[child_event, cause_event],
            failures=[child, cause],
        )


def test_checkpoint_parent_must_precede_child_cursor() -> None:
    spec, experiment, run = valid_graph()
    artifact_event = Event(
        event_id=EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=1,
        event_type=EventType.artifact_created,
        actor="runner",
        payload=ArtifactCreatedPayload(artifact_id=ARTIFACT_ID),
    )
    child_event = Event(
        event_id=SECOND_EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=2,
        event_type=EventType.checkpoint_created,
        actor="runner",
        payload=CheckpointCreatedPayload(checkpoint_id=CHECKPOINT_ID),
    )
    parent_event = Event(
        event_id=THIRD_EVENT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        sequence=3,
        event_type=EventType.checkpoint_created,
        actor="runner",
        payload=CheckpointCreatedPayload(checkpoint_id=PARENT_CHECKPOINT_ID),
    )
    manifest = Artifact(
        artifact_id=ARTIFACT_ID,
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        producer_event_id=artifact_event.event_id,
        producer="runner",
        artifact_type=ArtifactType.manifest,
        role="checkpoint_manifest",
        filename="manifest.json",
        storage_uri="artifacts/manifest.json",
        content_hash=HASH_A,
        media_type="application/json",
        size_bytes=1,
        owner=run.owner,
    )

    def make_checkpoint(
        checkpoint_id: str,
        event_id: str,
        sequence: int,
        parent_id: str | None = None,
    ) -> Checkpoint:
        record = Checkpoint(
            checkpoint_id=checkpoint_id,
            experiment_id=experiment.experiment_id,
            run_id=run.run_id,
            event_id=event_id,
            event_sequence=sequence,
            stage="analysis",
            turn=0,
            parent_checkpoint_id=parent_id,
            components=[CheckpointComponent.artifact_manifest],
            artifact_manifest_id=manifest.artifact_id,
            spec_hash=run.spec_hash,
            code_commit=COMMIT,
            container_digest=run.container.image.content_hash,
            model_identity=f"{run.resolved_model.provider}:{run.resolved_model.model}",
            integrity_hash=HASH_A,
        )
        return record.model_copy(
            update={"integrity_hash": checkpoint_integrity_hash(record)}
        )

    child = make_checkpoint(
        CHECKPOINT_ID, child_event.event_id, 2, PARENT_CHECKPOINT_ID
    )
    parent = make_checkpoint(PARENT_CHECKPOINT_ID, parent_event.event_id, 3)
    run = run.model_copy(
        update={
            "event_sequence": 3,
            "artifact_ids": (manifest.artifact_id,),
            "checkpoint_ids": (child.checkpoint_id, parent.checkpoint_id),
        }
    )
    with pytest.raises(GraphIntegrityError, match="missing parent"):
        validate_record_graph(
            spec=spec,
            experiment=experiment,
            runs=[run],
            events=[artifact_event, child_event, parent_event],
            artifacts=[manifest],
            checkpoints=[child, parent],
        )
