from __future__ import annotations

import json
from pathlib import Path

import pytest

import caribou.control.store as store_module
from caribou.control.api import ControlError
from caribou.control.records import ArtifactManifest
from caribou.control.service import ExperimentService
from caribou.control.specs import (
    ADAPTER_PARAMETER,
    AGENT_PATH_SMOKE_ADAPTER,
    LOCAL_LIFECYCLE_ADAPTER,
)
from caribou.control.store import (
    SUPPORTED_RESUME_REQUIREMENTS,
    ExperimentStore,
)
from caribou.control.worker import execute as execute_worker
from caribou.domain.enums import (
    ArtifactType,
    CheckpointComponent,
    EventType,
    ExperimentState,
    InterfaceOrigin,
    MemoryStrategy,
    RunState,
)
from caribou.domain.lifecycle import create_resume_attempt
from caribou.domain.models import ExperimentSpec, HeartbeatPayload
from caribou.domain.serialization import (
    initialize_run_journal,
    read_run_journal,
    write_model,
)

from .test_control_agent_workload import _workload_spec
from .test_domain_models import make_spec


def _running_store(tmp_path: Path) -> tuple[ExperimentStore, str]:
    spec = _workload_spec(tmp_path, AGENT_PATH_SMOKE_ADAPTER)
    store = ExperimentStore(tmp_path / "store")
    run_id = store.submit(spec, "checkpoint-source").runs[0].run_id
    store.transition_run(
        run_id,
        RunState.starting,
        reason="test worker started",
        actor="test-worker",
    )
    store.transition_run(
        run_id,
        RunState.running,
        reason="deterministic workload initialized",
        actor="test-worker",
    )
    store.append_run_event(
        run_id,
        event_type=EventType.heartbeat,
        payload=HeartbeatPayload(message="turn completed"),
        actor="agent-runner",
        turn=1,
        current_agent="analyst",
        stage="completed_turn",
    )
    return store, run_id


def _component_artifacts(
    store: ExperimentStore, run_id: str
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    roles = {
        "dataset": "checkpoint_dataset_state",
        "messages": "checkpoint_message_history",
        "agent": "checkpoint_agent_state",
        "actions": "checkpoint_executed_actions",
    }
    payloads: dict[str, dict[str, object]] = {
        "dataset": {
            "schema_version": "test.dataset_state.v1",
            "binding": "/workspace/dataset.h5ad",
            "content_hash": "sha256:" + "d" * 64,
        },
        "messages": {
            "schema_version": "caribou.message_history.v1",
            "messages": [
                {"role": "user", "content": "analyze"},
                {"role": "assistant", "content": "completed turn one"},
            ],
        },
        "agent": {
            "schema_version": "caribou.agent_session_checkpoint_state.v1",
            "current_agent_name": "analyst",
            "turns_completed": 1,
            "next_turn": 2,
        },
        "actions": {
            "schema_version": "caribou.executed_actions.v1",
            "actions": [
                {
                    "action_id": f"{run_id}:turn:1:block:1",
                    "status": "completed",
                }
            ],
        },
    }
    artifacts = {}
    for name in ("dataset", "messages", "agent", "actions"):
        artifacts[name] = store.record_json_artifact(
            run_id,
            filename=f"checkpoint-{name}.json",
            role=roles[name],
            value=payloads[name],
            producer="checkpoint-writer",
            artifact_type=ArtifactType.checkpoint,
            schema_type=f"test.checkpoint_{name}",
            schema_version_name="v1",
            turn=1,
            current_agent="analyst",
        )

    payloads["manifest"] = {
        "schema_version": "caribou.checkpoint_artifact_manifest.v1",
        "frontier": [
            {
                "artifact_id": artifacts[name].artifact_id,
                "role": artifacts[name].role,
                "content_hash": artifacts[name].content_hash,
            }
            for name in ("dataset", "messages", "agent", "actions")
        ],
    }
    artifacts["manifest"] = store.record_json_artifact(
        run_id,
        filename="checkpoint-artifact-manifest.json",
        role="checkpoint_artifact_manifest",
        value=payloads["manifest"],
        producer="checkpoint-writer",
        artifact_type=ArtifactType.checkpoint,
        schema_type="caribou.checkpoint_artifact_manifest",
        schema_version_name="v1",
        turn=1,
        current_agent="analyst",
    )
    return (
        {name: artifact.artifact_id for name, artifact in artifacts.items()},
        payloads,
    )


def _request_and_record(
    store: ExperimentStore,
    run_id: str,
    component_ids: dict[str, str],
):
    store.request_checkpoint(
        run_id,
        idempotency_key="checkpoint-turn-one",
        actor="test-controller",
        reason="controlled interruption after turn one",
    )
    return store.record_checkpoint(
        run_id,
        stage="completed_turn",
        turn=1,
        current_agent="analyst",
        dataset_artifact_id=component_ids["dataset"],
        message_history_artifact_id=component_ids["messages"],
        agent_state_artifact_id=component_ids["agent"],
        executed_actions_artifact_id=component_ids["actions"],
        artifact_manifest_id=component_ids["manifest"],
        resume_requirements=sorted(SUPPORTED_RESUME_REQUIREMENTS),
        actor="checkpoint-writer",
    )


def _resumable_source(tmp_path: Path):
    store, run_id = _running_store(tmp_path)
    component_ids, payloads = _component_artifacts(store, run_id)
    checkpoint = _request_and_record(store, run_id, component_ids)
    source, applied = store.transition_run(
        run_id,
        RunState.resumable,
        reason="controlled interruption committed",
        actor="test-worker",
        checkpoint=checkpoint,
    )
    assert applied is True
    assert source.state == RunState.resumable
    return store, source, checkpoint, component_ids, payloads


def test_checkpoint_request_is_idempotent_and_key_conflicts(tmp_path: Path) -> None:
    store, run_id = _running_store(tmp_path)

    run, request, applied = store.request_checkpoint(
        run_id,
        idempotency_key="turn-one",
        actor="controller",
        reason="checkpoint now",
    )
    replay_run, replay, replay_applied = store.request_checkpoint(
        run_id,
        idempotency_key="turn-one",
        actor="another-controller",
        reason="same durable request",
    )

    assert run.run_id == replay_run.run_id == run_id
    assert applied is True
    assert replay_applied is False
    assert replay == request == store.checkpoint_request(run_id)

    with pytest.raises(ControlError) as exc_info:
        store.request_checkpoint(
            run_id,
            idempotency_key="different-request",
            actor="controller",
            reason="conflicting request",
        )
    assert exc_info.value.code == "CHECKPOINT_REQUEST_CONFLICT"


def test_checkpoint_request_rejects_non_full_memory(tmp_path: Path) -> None:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={
            "memory": base.conditions[0].memory.model_copy(
                update={"strategy": MemoryStrategy.episodic}
            ),
            "parameters": {ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER},
        }
    )
    spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [condition], "repetitions": 1}
        ).model_dump_json()
    )
    store = ExperimentStore(tmp_path / "store")
    run_id = store.submit(spec, "unsupported-memory").runs[0].run_id

    with pytest.raises(ControlError) as exc_info:
        store.request_checkpoint(
            run_id,
            idempotency_key="episodic-checkpoint",
            actor="controller",
            reason="unsupported memory mode",
        )

    assert exc_info.value.code == "CHECKPOINT_MEMORY_UNSUPPORTED"
    assert store.checkpoint_request(run_id) is None


def test_checkpoint_request_rejects_non_agent_adapter(tmp_path: Path) -> None:
    base = make_spec()
    condition = base.conditions[0].model_copy(
        update={"parameters": {ADAPTER_PARAMETER: LOCAL_LIFECYCLE_ADAPTER}}
    )
    spec = ExperimentSpec.model_validate_json(
        base.model_copy(
            update={"conditions": [condition], "repetitions": 1}
        ).model_dump_json()
    )
    store = ExperimentStore(tmp_path / "store")
    run_id = store.submit(spec, "unsupported-adapter").runs[0].run_id

    with pytest.raises(ControlError) as exc_info:
        store.request_checkpoint(
            run_id,
            idempotency_key="lifecycle-checkpoint",
            actor="controller",
            reason="unsupported adapter",
        )

    assert exc_info.value.code == "CHECKPOINT_ADAPTER_UNSUPPORTED"
    assert store.checkpoint_request(run_id) is None


def test_checkpoint_components_event_link_and_state_are_durable(tmp_path: Path) -> None:
    store, run_id = _running_store(tmp_path)
    component_ids, payloads = _component_artifacts(store, run_id)
    before = store.run(run_id)

    checkpoint = _request_and_record(store, run_id, component_ids)

    restarted = ExperimentStore(store.root)
    run = restarted.run(run_id)
    journal = read_run_journal(restarted.run_journal_path(run_id))
    assert run.state == RunState.checkpointed
    assert run.checkpoint_ids == [checkpoint.checkpoint_id]
    assert journal.checkpoints == [checkpoint]
    assert restarted.checkpoints(run_id) == (checkpoint,)
    assert restarted.checkpoint(run_id, checkpoint.checkpoint_id) == checkpoint
    assert checkpoint.event_sequence == before.event_sequence + 1
    assert journal.events[-2].event_type == EventType.checkpoint_created
    assert journal.events[-2].event_id == checkpoint.event_id
    assert journal.events[-1].event_type == EventType.state_transition
    assert journal.events[-1].payload.to_state == RunState.checkpointed
    assert [event.sequence for event in journal.events] == list(
        range(1, run.event_sequence + 1)
    )
    assert checkpoint.components == [
        CheckpointComponent.dataset_state,
        CheckpointComponent.message_history,
        CheckpointComponent.agent_state,
        CheckpointComponent.executed_actions,
        CheckpointComponent.artifact_manifest,
    ]
    assert checkpoint.dataset_artifact_id == component_ids["dataset"]
    assert checkpoint.message_history_artifact_id == component_ids["messages"]
    assert checkpoint.agent_state_artifact_id == component_ids["agent"]
    assert checkpoint.executed_actions_artifact_id == component_ids["actions"]
    assert checkpoint.artifact_manifest_id == component_ids["manifest"]
    assert set(checkpoint.resume_requirements) == SUPPORTED_RESUME_REQUIREMENTS

    by_id = {
        artifact.artifact_id: artifact
        for artifact in restarted.verify_artifacts(run_id)
    }
    for name, artifact_id in component_ids.items():
        value = json.loads(
            restarted.artifact_path(by_id[artifact_id]).read_text(encoding="utf-8")
        )
        assert value == payloads[name]


def test_checkpoint_publication_failure_is_atomic_and_transition_retry_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run_id = _running_store(tmp_path)
    component_ids, _ = _component_artifacts(store, run_id)
    store.request_checkpoint(
        run_id,
        idempotency_key="checkpoint-turn-one",
        actor="controller",
        reason="controlled checkpoint",
    )
    journal_before = store.run_journal_path(run_id).read_bytes()
    real_commit = store_module.commit_run_checkpoint

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("injected checkpoint journal failure")

    monkeypatch.setattr(store_module, "commit_run_checkpoint", fail_commit)
    with pytest.raises(RuntimeError, match="injected checkpoint journal failure"):
        _request_and_record(store, run_id, component_ids)
    assert store.run_journal_path(run_id).read_bytes() == journal_before
    assert store.checkpoints(run_id) == ()
    assert store.run(run_id).state == RunState.running

    monkeypatch.setattr(store_module, "commit_run_checkpoint", real_commit)
    real_transition = ExperimentStore._transition_run_unlocked
    fail_once = True

    def fail_transition(self, target_run_id, target, **kwargs):
        nonlocal fail_once
        if target == RunState.checkpointed and fail_once:
            fail_once = False
            raise RuntimeError("injected checkpoint transition failure")
        return real_transition(self, target_run_id, target, **kwargs)

    monkeypatch.setattr(ExperimentStore, "_transition_run_unlocked", fail_transition)
    with pytest.raises(RuntimeError, match="injected checkpoint transition failure"):
        _request_and_record(store, run_id, component_ids)
    partial = store.checkpoints(run_id)
    assert len(partial) == 1
    assert store.run(run_id).state == RunState.running
    assert [event.event_type for event in store.events(run_id)].count(
        EventType.checkpoint_created
    ) == 1

    monkeypatch.setattr(ExperimentStore, "_transition_run_unlocked", real_transition)
    repaired = _request_and_record(store, run_id, component_ids)
    assert repaired == partial[0]
    assert store.run(run_id).state == RunState.checkpointed
    assert store.run(run_id).checkpoint_ids == [repaired.checkpoint_id]
    assert [event.event_type for event in store.events(run_id)].count(
        EventType.checkpoint_created
    ) == 1


def test_worker_restart_rolls_committed_running_checkpoint_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, run_id = _running_store(tmp_path)
    component_ids, _ = _component_artifacts(store, run_id)
    real_transition = ExperimentStore._transition_run_unlocked

    def fail_checkpoint_transition(self, target_run_id, target, **kwargs):
        if target == RunState.checkpointed:
            raise RuntimeError("injected death after checkpoint commit")
        return real_transition(self, target_run_id, target, **kwargs)

    monkeypatch.setattr(
        ExperimentStore, "_transition_run_unlocked", fail_checkpoint_transition
    )
    with pytest.raises(RuntimeError, match="injected death after checkpoint commit"):
        _request_and_record(store, run_id, component_ids)
    assert store.run(run_id).state == RunState.running
    assert len(store.checkpoints(run_id)) == 1

    monkeypatch.setattr(ExperimentStore, "_transition_run_unlocked", real_transition)
    restarted = ExperimentStore(store.root)
    assert execute_worker(restarted, run_id) == 0
    recovered = restarted.run(run_id)
    assert recovered.state == RunState.resumable
    assert recovered.resume_eligible is True
    assert recovered.current_turn == 1


def test_checkpoint_replay_is_idempotent_but_component_role_swap_conflicts(
    tmp_path: Path,
) -> None:
    store, run_id = _running_store(tmp_path)
    component_ids, _ = _component_artifacts(store, run_id)
    checkpoint = _request_and_record(store, run_id, component_ids)
    journal_before = store.run_journal_path(run_id).read_bytes()

    replay = _request_and_record(store, run_id, component_ids)
    assert replay == checkpoint
    assert store.run_journal_path(run_id).read_bytes() == journal_before

    swapped = dict(component_ids)
    swapped["dataset"], swapped["messages"] = (
        swapped["messages"],
        swapped["dataset"],
    )
    with pytest.raises(ControlError) as exc_info:
        _request_and_record(store, run_id, swapped)
    assert exc_info.value.code == "CHECKPOINT_REPLAY_CONFLICT"
    assert store.run_journal_path(run_id).read_bytes() == journal_before


def test_checkpoint_publication_rejects_component_role_swap(tmp_path: Path) -> None:
    store, run_id = _running_store(tmp_path)
    component_ids, _ = _component_artifacts(store, run_id)
    store.request_checkpoint(
        run_id,
        idempotency_key="role-swap",
        actor="controller",
        reason="validate semantic roles",
    )
    swapped = dict(component_ids)
    swapped["dataset"], swapped["messages"] = (
        swapped["messages"],
        swapped["dataset"],
    )

    with pytest.raises(ControlError) as exc_info:
        store.record_checkpoint(
            run_id,
            stage="completed_turn",
            turn=1,
            current_agent="analyst",
            dataset_artifact_id=swapped["dataset"],
            message_history_artifact_id=swapped["messages"],
            agent_state_artifact_id=swapped["agent"],
            executed_actions_artifact_id=swapped["actions"],
            artifact_manifest_id=swapped["manifest"],
            resume_requirements=sorted(SUPPORTED_RESUME_REQUIREMENTS),
            actor="checkpoint-writer",
        )

    assert exc_info.value.code == "CHECKPOINT_COMPONENT_ROLE_INVALID"
    assert store.checkpoints(run_id) == ()
    assert store.run(run_id).state == RunState.running


def test_resume_is_single_child_idempotent_and_preserves_source(tmp_path: Path) -> None:
    store, source, checkpoint, _, _ = _resumable_source(tmp_path)
    experiment_id = source.experiment_id
    source_journal = store.run_journal_path(source.run_id).read_bytes()
    source_manifest = store.artifact_manifest_path(source.run_id).read_bytes()
    source_events = store.events(source.run_id)
    source_artifacts = store.verify_artifacts(source.run_id)

    assert store.reconcile_experiment(experiment_id).state == ExperimentState.active
    first = store.resume(
        source.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        idempotency_key="resume-turn-one",
        interface=InterfaceOrigin.cli,
    )
    replay = store.resume(
        source.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        idempotency_key="resume-turn-one",
        interface=InterfaceOrigin.cli,
    )

    child = first.child
    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert replay.child == child
    assert child.state == RunState.queued
    assert child.attempt_index == source.attempt_index + 1
    assert child.resumed_from_run_id == source.run_id
    assert child.resume_checkpoint_id == checkpoint.checkpoint_id
    assert child.current_turn == source.current_turn == checkpoint.turn
    assert child.current_agent == source.current_agent == "analyst"
    assert child.artifact_ids == []
    assert store.artifact_manifest(child.run_id).artifacts == ()
    experiment = store.experiment(experiment_id)
    assert experiment.run_ids.count(child.run_id) == 1
    assert store.index().experiments[experiment_id].count(child.run_id) == 1
    assert store.index().runs[child.run_id] == experiment_id
    assert store.run_journal_path(source.run_id).read_bytes() == source_journal
    assert store.artifact_manifest_path(source.run_id).read_bytes() == source_manifest
    assert store.events(source.run_id) == source_events
    assert store.verify_artifacts(source.run_id) == source_artifacts
    assert store.reconcile_experiment(experiment_id).state == ExperimentState.active

    with pytest.raises(ControlError) as exc_info:
        store.resume(
            source.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            idempotency_key="second-child-forbidden",
        )
    assert exc_info.value.code == "CHECKPOINT_ALREADY_RESUMED"

    store.transition_run(
        child.run_id,
        RunState.starting,
        reason="resumed worker started",
        actor="test-worker",
    )
    store.transition_run(
        child.run_id,
        RunState.running,
        reason="checkpoint restored",
        actor="test-worker",
    )
    store.transition_run(
        child.run_id,
        RunState.succeeded,
        reason="resumed attempt completed",
        actor="test-worker",
        exit_code=0,
    )
    assert store.reconcile_experiment(experiment_id).state == ExperimentState.completed
    comparison = ExperimentService(store=store).compare(experiment_id)
    assert comparison["status"] == "complete"
    assert comparison["leaf_run_ids"] == [child.run_id]
    assert comparison["superseded_run_ids"] == [source.run_id]
    assert comparison["attempt_count"] == 2
    assert comparison["conditions"][0]["outcome_counts"] == {"succeeded": 1}
    assert comparison["attempts"][0]["superseded_by_run_id"] == child.run_id
    assert comparison["attempts"][1]["resumed_from_run_id"] == source.run_id
    assert store.run_journal_path(source.run_id).read_bytes() == source_journal
    assert store.artifact_manifest_path(source.run_id).read_bytes() == source_manifest


def test_tampered_checkpoint_component_fails_before_child_creation(
    tmp_path: Path,
) -> None:
    store, source, checkpoint, component_ids, _ = _resumable_source(tmp_path)
    artifacts = {
        artifact.artifact_id: artifact
        for artifact in store.artifact_manifest(source.run_id).artifacts
    }
    store.artifact_path(artifacts[component_ids["agent"]]).write_text(
        '{"tampered":true}\n', encoding="utf-8"
    )
    index_before = store.index()
    experiment_before = store.experiment(source.experiment_id)
    child_id = store._resume_run_id("tamper-must-not-create-child")

    with pytest.raises(ControlError) as exc_info:
        store.resume(
            source.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            idempotency_key="tamper-must-not-create-child",
        )
    assert exc_info.value.code == "ARTIFACT_INTEGRITY_ERROR"
    assert not store.run_journal_path(child_id).exists()
    assert store.index() == index_before
    assert store.experiment(source.experiment_id) == experiment_before


def test_resume_repairs_pre_index_child_without_duplicate_lineage(
    tmp_path: Path,
) -> None:
    store, source, checkpoint, _, _ = _resumable_source(tmp_path)
    key = "recover-pre-index-child"
    child_id = store._resume_run_id(key)
    child = create_resume_attempt(
        source,
        checkpoint=checkpoint,
        idempotency_key=key,
        run_id=child_id,
        interface=InterfaceOrigin.cli,
    )
    store.run_dir(child_id).mkdir(parents=True, mode=0o700)
    initialize_run_journal(store.run_journal_path(child_id), child)
    write_model(
        store.artifact_manifest_path(child_id),
        ArtifactManifest(run_id=child_id),
    )
    assert child_id not in store.experiment(source.experiment_id).run_ids
    assert child_id not in store.index().runs

    recovered = store.resume(
        source.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        idempotency_key=key,
    )

    assert recovered.idempotent_replay is True
    assert recovered.child.run_id == child_id
    assert recovered.child.state == RunState.queued
    assert store.experiment(source.experiment_id).run_ids.count(child_id) == 1
    assert store.index().experiments[source.experiment_id].count(child_id) == 1
    assert store.index().runs[child_id] == source.experiment_id
