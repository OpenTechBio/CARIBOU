from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import caribou.control.agent_workload as agent_workload
from caribou.control.api import ControlError
from caribou.control.records import ArtifactManifest
from caribou.control.specs import AGENT_PATH_SMOKE_ADAPTER
from caribou.control.store import ExperimentStore
from caribou.control.worker import execute as execute_worker
from caribou.domain.enums import (
    CheckpointComponent,
    EventType,
    ExperimentState,
    RunState,
)
from caribou.domain.models import (
    Artifact,
    Checkpoint,
    ExperimentSpec,
    StateTransitionPayload,
    checkpoint_integrity_hash,
)
from caribou.domain.serialization import file_hash, write_model
from caribou.execution.runner import RunnerEvent

from .test_control_agent_workload import _local_reference, _workload_spec


_SMOKE_CODE = 'print("CARIBOU_AGENT_PATH_OK")'
_CHECKPOINT_ROLES = {
    "dataset": "checkpoint_dataset_state",
    "messages": "checkpoint_message_history",
    "state": "checkpoint_agent_state",
    "actions": "checkpoint_executed_actions",
    "manifest": "checkpoint_artifact_manifest",
}


def test_checkpoint_event_ledger_reads_beyond_ten_thousand_event_page() -> None:
    events = [SimpleNamespace(sequence=sequence) for sequence in range(1, 10_002)]

    class PagedStore:
        def __init__(self) -> None:
            self.after_values: list[int] = []

        def events(self, _run_id: str, *, after: int, limit: int):
            self.after_values.append(after)
            return tuple(event for event in events if event.sequence > after)[:limit]

    store = PagedStore()
    ledger = agent_workload._durable_event_ledger(  # type: ignore[arg-type]
        store, "run_" + "0" * 32
    )

    assert [event.sequence for event in ledger] == list(range(1, 10_002))
    assert store.after_values == [0, 10_000]


def _smoke_spec(tmp_path: Path) -> ExperimentSpec:
    spec = _workload_spec(tmp_path, AGENT_PATH_SMOKE_ADAPTER)
    blueprint_path = tmp_path / f"{AGENT_PATH_SMOKE_ADAPTER}-blueprint.json"
    blueprint_path.write_text(
        json.dumps(
            {
                "global_policy": "Complete the deterministic checkpoint probe.",
                "agents": {
                    "analyst": {
                        "prompt": "Delegate this bounded probe to general.",
                        "neighbors": {
                            "delegate_to_general": {
                                "target_agent": "general",
                                "description": "Delegate the probe to general.",
                            }
                        },
                        "code_samples": [],
                        "rag": {"enabled": False},
                    },
                    "general": {
                        "prompt": "Execute the bounded probe and finish.",
                        "neighbors": {},
                        "code_samples": [],
                        "rag": {"enabled": False},
                    },
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    condition = spec.conditions[0]
    blueprint = condition.blueprint.model_copy(
        update={
            "source": _local_reference(blueprint_path, "application/json"),
            "driver_agent": "analyst",
        }
    )
    condition = condition.model_copy(update={"blueprint": blueprint})
    return ExperimentSpec.model_validate_json(
        spec.model_copy(update={"conditions": [condition]}).model_dump_json()
    )


def _checkpoint_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ExperimentStore, str]:
    spec = _smoke_spec(tmp_path)
    store = ExperimentStore(tmp_path / "checkpoint-workload-store")
    source_id = store.submit(spec, "checkpoint-workload-source").runs[0].run_id

    monkeypatch.setattr(
        agent_workload,
        "_verify_code_identity",
        lambda _expected_commit, _adapter, **_kwargs: None,
    )
    durable_recorder = agent_workload._event_recorder
    requested = False

    def request_at_turn_two(
        target_store: ExperimentStore, target_run_id: str
    ) -> Callable[[RunnerEvent], None]:
        record = durable_recorder(target_store, target_run_id)

        def record_and_request(event: RunnerEvent) -> None:
            nonlocal requested
            record(event)
            if (
                not requested
                and target_run_id == source_id
                and event["event_type"] == "code_result"
                and event["turn"] == 2
            ):
                _, _, applied = target_store.request_checkpoint(
                    source_id,
                    idempotency_key="checkpoint-after-turn-two",
                    actor="test-controller",
                    reason="deterministic checkpoint after the turn-two action",
                )
                assert applied is True
                requested = True

        return record_and_request

    monkeypatch.setattr(agent_workload, "_event_recorder", request_at_turn_two)

    assert execute_worker(store, source_id) == 0
    assert requested is True
    assert store.run(source_id).state == RunState.resumable
    return store, source_id


def _checkpoint_artifacts(
    store: ExperimentStore, source_id: str
) -> tuple[Checkpoint, dict[str, Artifact]]:
    checkpoint = store.checkpoints(source_id)[0]
    manifest = store.artifact_manifest(source_id)
    artifact_ids = {
        "dataset": checkpoint.dataset_artifact_id,
        "messages": checkpoint.message_history_artifact_id,
        "state": checkpoint.agent_state_artifact_id,
        "actions": checkpoint.executed_actions_artifact_id,
        "manifest": checkpoint.artifact_manifest_id,
    }
    artifacts: dict[str, Artifact] = {}
    for name, artifact_id in artifact_ids.items():
        artifact = manifest.artifact(str(artifact_id))
        assert artifact is not None
        artifacts[name] = artifact
    return checkpoint, artifacts


def _json_component(store: ExperimentStore, artifact: Artifact) -> dict[str, object]:
    path = store.artifact_path(artifact)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_agent_workload_checkpoints_at_turn_two_and_resumes_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source_id = _checkpoint_source(tmp_path, monkeypatch)
    source = store.run(source_id)
    checkpoint, artifacts = _checkpoint_artifacts(store, source_id)

    assert checkpoint.turn == source.current_turn == 2
    assert checkpoint.stage == "agent_turn_boundary"
    assert checkpoint.components == [
        CheckpointComponent.dataset_state,
        CheckpointComponent.message_history,
        CheckpointComponent.agent_state,
        CheckpointComponent.executed_actions,
        CheckpointComponent.artifact_manifest,
    ]
    assert checkpoint.integrity_hash == checkpoint_integrity_hash(checkpoint)
    assert {
        name: artifact.role for name, artifact in artifacts.items()
    } == _CHECKPOINT_ROLES
    verified_ids = {
        artifact.artifact_id for artifact in store.verify_artifacts(source_id)
    }
    for artifact in artifacts.values():
        assert artifact.artifact_id in verified_ids
        assert file_hash(store.artifact_path(artifact)) == artifact.content_hash

    transitions = [
        event.payload.to_state
        for event in store.events(source_id, limit=10000)
        if event.event_type == EventType.state_transition
        and isinstance(event.payload, StateTransitionPayload)
    ]
    assert transitions[-2:] == [RunState.checkpointed, RunState.resumable]

    messages = _json_component(store, artifacts["messages"])
    state = _json_component(store, artifacts["state"])
    actions = _json_component(store, artifacts["actions"])
    checkpoint_manifest = _json_component(store, artifacts["manifest"])

    assert messages["run_id"] == source_id
    message_items = messages["messages"]
    assert isinstance(message_items, list)
    assistant_contents = [
        item["content"]
        for item in message_items
        if isinstance(item, dict) and item.get("role") == "assistant"
    ]
    assert assistant_contents.count("delegate_to_general") == 1
    assert assistant_contents.count(f"```python\n{_SMOKE_CODE}\n```") == 1

    state_payload = state["state"]
    assert isinstance(state_payload, dict)
    assert state_payload["turns_completed"] == 2
    assert state_payload["next_turn"] == 3
    assert state_payload["current_agent_name"] == source.current_agent == "general"
    assert state_payload["code_blocks_produced"] == 1
    assert state_payload["code_exec_attempts"] == 1
    assert state_payload["code_exec_failures"] == 0
    assert isinstance(state_payload["action_space_past_actions"], list)

    action_events = [
        event.model_dump(mode="json")
        for event in store.events(source_id, limit=10_000)
        if event.event_type
        in {
            EventType.agent_switch,
            EventType.code_submitted,
            EventType.code_result,
        }
    ]
    assert [event["event_type"] for event in action_events] == [
        EventType.agent_switch.value,
        EventType.code_submitted.value,
        EventType.code_result.value,
    ]
    submitted, result = action_events[-2:]
    assert submitted["turn"] == result["turn"] == actions["through_turn"] == 2
    assert submitted["payload"]["action_id"] == result["payload"]["action_id"]
    assert result["payload"]["success"] is True
    assert actions["event_ids"] == [event["event_id"] for event in action_events]
    assert actions["events_hash"] == agent_workload._checkpoint_value_hash(
        action_events
    )
    action_frontier = actions["through_event_sequence"]
    assert isinstance(action_frontier, int) and not isinstance(action_frontier, bool)
    assert action_frontier < checkpoint.event_sequence

    expected_components = [
        agent_workload._checkpoint_component_reference(artifacts[name])
        for name in ("dataset", "messages", "state", "actions")
    ]
    assert checkpoint_manifest["run_id"] == source_id
    assert checkpoint_manifest["components"] == expected_components
    manifest_frontier = checkpoint_manifest["frontier_event_sequence"]
    assert isinstance(manifest_frontier, int) and not isinstance(
        manifest_frontier, bool
    )
    assert manifest_frontier < checkpoint.event_sequence

    source_journal = store.run_journal_path(source_id).read_bytes()
    source_artifact_manifest = store.artifact_manifest_path(source_id).read_bytes()
    source_events = store.events(source_id, limit=10000)
    source_hashes = {
        artifact.artifact_id: file_hash(store.artifact_path(artifact))
        for artifact in store.verify_artifacts(source_id)
    }

    resumed = store.resume(
        source_id,
        checkpoint_id=checkpoint.checkpoint_id,
        idempotency_key="resume-checkpoint-workload",
    )
    child_id = resumed.child.run_id
    assert resumed.child.state == RunState.queued
    assert execute_worker(store, child_id) == 0

    child = store.run(child_id)
    assert child.state == RunState.succeeded
    child_events = store.events(child_id, limit=10000)
    agent_turns = [
        event.turn
        for event in child_events
        if event.event_type == EventType.heartbeat and event.stage == "agent_turn"
    ]
    assert agent_turns == [3]
    assert not any(
        event.event_type == EventType.code_submitted for event in child_events
    )
    assert (
        sum(event.event_type == EventType.code_submitted for event in source_events)
        == 1
    )
    child_result_artifact = next(
        artifact
        for artifact in store.artifact_manifest(child_id).artifacts
        if artifact.role == "agent_session_result"
    )
    child_result = _json_component(store, child_result_artifact)
    assert child_result["succeeded"] is True
    assert child_result["final_turn"] == child_result["turns_completed"] == 3
    assert child_result["code_exec_attempts"] == 1

    assert store.run_journal_path(source_id).read_bytes() == source_journal
    assert (
        store.artifact_manifest_path(source_id).read_bytes() == source_artifact_manifest
    )
    assert store.events(source_id, limit=10000) == source_events
    assert {
        artifact.artifact_id: file_hash(store.artifact_path(artifact))
        for artifact in store.verify_artifacts(source_id)
    } == source_hashes
    assert store.experiment(source.experiment_id).state == ExperimentState.completed


def test_checkpoint_manifest_mismatch_fails_before_provider_or_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, source_id = _checkpoint_source(tmp_path, monkeypatch)
    checkpoint, artifacts = _checkpoint_artifacts(store, source_id)
    resumed = store.resume(
        source_id,
        checkpoint_id=checkpoint.checkpoint_id,
        idempotency_key="resume-tampered-checkpoint-workload",
    )
    child_id = resumed.child.run_id
    store.transition_run(
        child_id,
        RunState.starting,
        reason="tamper test worker started",
        actor="test-worker",
    )

    manifest_artifact = artifacts["manifest"]
    checkpoint_manifest_path = store.artifact_path(manifest_artifact)
    checkpoint_manifest = json.loads(
        checkpoint_manifest_path.read_text(encoding="utf-8")
    )
    checkpoint_manifest["components"][0]["role"] = "mismatched_dataset_role"
    checkpoint_manifest_path.write_text(
        json.dumps(
            checkpoint_manifest,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    durable_manifest = store.artifact_manifest(source_id)
    replacement = manifest_artifact.model_copy(
        update={
            "content_hash": file_hash(checkpoint_manifest_path),
            "size_bytes": checkpoint_manifest_path.stat().st_size,
        }
    )
    updated_manifest = ArtifactManifest.model_validate(
        {
            **durable_manifest.model_dump(mode="python"),
            "artifacts": tuple(
                replacement
                if artifact.artifact_id == replacement.artifact_id
                else artifact
                for artifact in durable_manifest.artifacts
            ),
        }
    )
    write_model(store.artifact_manifest_path(source_id), updated_manifest)
    store.verify_artifacts(source_id)

    touched: list[str] = []

    def unexpected_provider(*args: object, **kwargs: object) -> object:
        touched.append("provider")
        raise AssertionError((args, kwargs))

    def unexpected_sandbox(*args: object, **kwargs: object) -> object:
        touched.append("sandbox")
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(agent_workload, "_ScriptedClient", unexpected_provider)
    monkeypatch.setattr(agent_workload, "_RecordingSandbox", unexpected_sandbox)

    with pytest.raises(ControlError) as exc_info:
        agent_workload.execute_agent_workload(
            store,
            child_id,
            adapter=AGENT_PATH_SMOKE_ADAPTER,
            actor="test-worker",
        )

    assert exc_info.value.code == "CHECKPOINT_MANIFEST_INVALID"
    assert touched == []
    assert store.run(child_id).state == RunState.starting
