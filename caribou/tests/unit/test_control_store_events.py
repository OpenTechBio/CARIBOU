from pathlib import Path

import pytest

from caribou.control.api import ControlError
from caribou.control.specs import ADAPTER_PARAMETER, LOCAL_LIFECYCLE_ADAPTER
from caribou.control.store import ExperimentStore
from caribou.domain.enums import ArtifactType, EventType, RunState
from caribou.domain.models import (
    CodeSubmittedPayload,
    ExperimentSpec,
    MessagePayload,
)

from .test_domain_models import make_spec


def running_store(tmp_path: Path) -> tuple[ExperimentStore, str]:
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
    run_id = store.submit(spec, "event-store-test").runs[0].run_id
    store.transition_run(
        run_id,
        RunState.starting,
        reason="test worker started",
        actor="test",
    )
    store.transition_run(
        run_id,
        RunState.running,
        reason="test workload ready",
        actor="test",
    )
    return store, run_id


def test_runner_events_and_general_artifacts_share_one_journal(tmp_path: Path) -> None:
    store, run_id = running_store(tmp_path)
    message = store.append_run_event(
        run_id,
        event_type=EventType.message,
        payload=MessagePayload(
            role="assistant",
            agent_name="analyst",
            content="running code",
        ),
        actor="agent-runner",
        turn=1,
        current_agent="analyst",
    )
    source = store.record_text_artifact(
        run_id,
        filename="turn-1-block-1.py",
        role="generated_code",
        text='print("ok")\n',
        producer="agent-runner",
        artifact_type=ArtifactType.code,
        media_type="text/x-python",
        turn=1,
        current_agent="analyst",
    )
    submitted = store.append_run_event(
        run_id,
        event_type=EventType.code_submitted,
        payload=CodeSubmittedPayload(
            action_id="turn-1-block-1",
            source_artifact_id=source.artifact_id,
            agent_name="analyst",
            block_index=1,
            total_blocks=1,
        ),
        actor="agent-runner",
        turn=1,
        current_agent="analyst",
    )
    generated = tmp_path / "generated.csv"
    generated.write_text("value\n1\n", encoding="utf-8")
    output = store.record_file_artifact(
        run_id,
        source=generated,
        filename="generated.csv",
        role="analysis_output",
        producer="agent-runner",
        artifact_type=ArtifactType.other,
        media_type="text/csv",
        turn=1,
        current_agent="analyst",
    )

    run = store.run(run_id)
    assert run.current_turn == 1
    assert run.current_agent == "analyst"
    assert run.artifact_ids == [source.artifact_id, output.artifact_id]
    assert message.sequence < submitted.sequence < run.event_sequence
    assert [event.sequence for event in store.events(run_id)] == list(
        range(1, run.event_sequence + 1)
    )
    assert {item.artifact_type for item in store.verify_artifacts(run_id)} == {
        ArtifactType.code,
        ArtifactType.other,
    }


def test_runner_event_turns_cannot_regress(tmp_path: Path) -> None:
    store, run_id = running_store(tmp_path)
    payload = MessagePayload(role="assistant", agent_name="analyst", content="one")
    store.append_run_event(
        run_id,
        event_type=EventType.message,
        payload=payload,
        actor="agent-runner",
        turn=2,
        current_agent="analyst",
    )

    with pytest.raises(ControlError) as exc_info:
        store.append_run_event(
            run_id,
            event_type=EventType.message,
            payload=payload,
            actor="agent-runner",
            turn=1,
            current_agent="analyst",
        )

    assert exc_info.value.code == "EVENT_TURN_REGRESSION"
