"""Crash-safety, integrity, and compare-and-swap tests for domain storage."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from caribou.domain.enums import EventType, ExperimentState, RunState
from caribou.domain.lifecycle import (
    RunTransition,
    transition_experiment,
    transition_run,
)
from caribou.domain.models import (
    Event,
    Experiment,
    HeartbeatPayload,
    ResourceRequest,
    StateTransitionPayload,
)
from caribou.domain.serialization import (
    ConcurrentUpdateError,
    IntegrityError,
    append_event,
    canonical_json_bytes,
    commit_run_transition,
    commit_experiment_transition,
    initialize_experiment_journal,
    initialize_run_journal,
    file_hash,
    model_hash,
    read_events,
    read_experiment_journal,
    read_model,
    read_run_journal,
    validate_event_stream,
    validate_run_event_pair,
    verify_artifact,
    write_model,
)

from .test_domain_models import EXP_ID, HASH_A, RUN_ID, make_run, make_spec


def heartbeat(sequence: int, *, run_id: str = RUN_ID) -> Event:
    return Event(
        experiment_id=EXP_ID,
        run_id=run_id,
        sequence=sequence,
        event_type=EventType.heartbeat,
        actor="runner",
        payload=HeartbeatPayload(message="alive"),
    )


def test_canonical_json_and_atomic_model_round_trip(tmp_path) -> None:
    model = ResourceRequest(memory_bytes=100, wall_seconds=10)
    assert canonical_json_bytes(model) == canonical_json_bytes(model)
    path = tmp_path / "resource.json"
    stored_hash = write_model(path, model)
    assert stored_hash == model_hash(model)
    assert read_model(path, ResourceRequest) == model
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / ".caribou-domain.lock").exists()


def test_spec_hash_is_stable_across_json_key_order_and_round_trip() -> None:
    spec = make_spec()
    document = spec.model_dump(mode="json")
    reversed_json = json.dumps(dict(reversed(list(document.items()))))
    restored = type(spec).model_validate_json(reversed_json)
    assert model_hash(restored) == model_hash(spec)


def test_compare_and_swap_rejects_stale_writer(tmp_path) -> None:
    path = tmp_path / "resource.json"
    first = ResourceRequest(memory_bytes=100, wall_seconds=10)
    current_hash = write_model(path, first)
    second = ResourceRequest(memory_bytes=200, wall_seconds=10)
    new_hash = write_model(path, second, expected_hash=current_hash)
    with pytest.raises(ConcurrentUpdateError):
        write_model(path, first, expected_hash=current_hash)
    assert read_model(path, ResourceRequest) == second
    assert new_hash != current_hash


def test_run_snapshot_and_event_are_committed_as_one_journal(tmp_path) -> None:
    path = tmp_path / "run-journal.json"
    run = make_run()
    journal_hash = initialize_run_journal(path, run)
    transition = transition_run(
        run, RunState.validated, reason="valid", actor="validator"
    )
    new_hash = commit_run_transition(path, transition, expected_hash=journal_hash)
    journal = read_run_journal(path)
    assert journal.run.state == RunState.validated
    assert journal.run.event_sequence == 1
    assert len(journal.events) == 1
    assert journal.events[0].sequence == 1
    with pytest.raises(ConcurrentUpdateError):
        commit_run_transition(path, transition, expected_hash=journal_hash)
    assert new_hash != journal_hash


def test_transition_commit_rejects_immutable_drift_and_spoofed_events(tmp_path) -> None:
    path = tmp_path / "run-journal.json"
    run = make_run()
    journal_hash = initialize_run_journal(path, run)
    valid = transition_run(run, RunState.validated, reason="valid", actor="validator")
    drifted = RunTransition(
        run=valid.run.model_copy(update={"owner": "another-user"}),
        event=valid.event,
        applied=True,
    )
    with pytest.raises(IntegrityError, match="immutable"):
        commit_run_transition(path, drifted, expected_hash=journal_hash)
    spoofed_event = valid.event.model_copy(
        update={
            "payload": StateTransitionPayload(
                from_state=RunState.planned,
                to_state=RunState.validated,
                reason="spoofed",
            )
        }
    )
    with pytest.raises(IntegrityError, match="legal snapshot"):
        commit_run_transition(
            path,
            RunTransition(run=valid.run, event=spoofed_event, applied=True),
            expected_hash=journal_hash,
        )


def test_experiment_transition_and_record_are_committed_atomically(tmp_path) -> None:
    path = tmp_path / "experiment-journal.json"
    experiment = Experiment(
        experiment_id=EXP_ID,
        spec_id="spec_" + "8" * 32,
        spec_version=1,
        spec_hash=HASH_A,
        owner="researcher",
    )
    journal_hash = initialize_experiment_journal(path, experiment)
    transition = transition_experiment(
        experiment,
        ExperimentState.validated,
        reason="specification validated",
        actor="reviewer",
    )
    commit_experiment_transition(path, transition, expected_hash=journal_hash)
    journal = read_experiment_journal(path)
    assert journal.experiment.state == ExperimentState.validated
    assert journal.experiment.transition_sequence == 1
    assert journal.transitions[0].to_state == ExperimentState.validated


def test_event_stream_rejects_gaps_duplicates_and_mixed_attempts(tmp_path) -> None:
    with pytest.raises(IntegrityError, match="gap"):
        validate_event_stream([heartbeat(2)])
    first = heartbeat(1)
    duplicate = heartbeat(2).model_copy(update={"event_id": first.event_id})
    with pytest.raises(IntegrityError, match="duplicate"):
        validate_event_stream([first, duplicate])
    with pytest.raises(IntegrityError, match="mixes"):
        validate_event_stream([first, heartbeat(2, run_id="run_" + "9" * 32)])
    path = tmp_path / "events.jsonl"
    append_event(path, first)
    append_event(path, heartbeat(2))
    assert [event.sequence for event in read_events(path)] == [1, 2]


def test_run_state_must_be_reconstructable_not_merely_sequence_matched() -> None:
    run = make_run(state=RunState.validated, event_sequence=1)
    with pytest.raises(IntegrityError, match="reconstructed"):
        validate_run_event_pair(run, [heartbeat(1)])


def test_corrupt_records_and_artifacts_fail_closed(tmp_path) -> None:
    record = tmp_path / "bad.json"
    record.write_text(json.dumps({"cpu_cores": "1"}), encoding="utf-8")
    with pytest.raises(IntegrityError):
        read_model(record, ResourceRequest)
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("evidence", encoding="utf-8")
    verify_artifact(artifact, file_hash(artifact), expected_size=8)
    with pytest.raises(IntegrityError, match="hash mismatch"):
        verify_artifact(artifact, "sha256:" + "0" * 64)
    link = tmp_path / "artifact-link.txt"
    link.symlink_to(artifact)
    with pytest.raises(IntegrityError, match="symlink"):
        verify_artifact(link, file_hash(artifact), root=tmp_path)


def test_failure_before_replace_preserves_previous_valid_record(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "resource.json"
    original = ResourceRequest(memory_bytes=100, wall_seconds=10)
    original_hash = write_model(path, original)

    def fail_replace(source, destination):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        write_model(
            path,
            ResourceRequest(memory_bytes=200, wall_seconds=10),
            expected_hash=original_hash,
        )
    assert read_model(path, ResourceRequest) == original
    assert not list(tmp_path.glob("*.tmp"))


def test_failure_after_replace_exposes_new_complete_record(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "resource.json"
    original = ResourceRequest(memory_bytes=100, wall_seconds=10)
    original_hash = write_model(path, original)
    replacement = ResourceRequest(memory_bytes=200, wall_seconds=10)
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory sync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="directory sync"):
        write_model(path, replacement, expected_hash=original_hash)
    # Replacement had already occurred; readers still see valid canonical JSON.
    assert read_model(path, ResourceRequest) == replacement


def test_concurrent_compare_and_swap_has_one_winner_and_no_lost_update(
    tmp_path,
) -> None:
    path = tmp_path / "resource.json"
    expected = write_model(path, ResourceRequest(memory_bytes=100, wall_seconds=10))

    def writer(memory: int):
        try:
            return write_model(
                path,
                ResourceRequest(memory_bytes=memory, wall_seconds=10),
                expected_hash=expected,
            )
        except ConcurrentUpdateError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(writer, (200, 300)))
    assert outcomes.count("conflict") == 1
    assert read_model(path, ResourceRequest).memory_bytes in (200, 300)


def test_incomplete_jsonl_tail_is_rejected_and_abandoned_temp_is_ignored(
    tmp_path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(canonical_json_bytes(heartbeat(1)) + b'\n{"incomplete":')
    with pytest.raises(IntegrityError, match="line 2"):
        read_events(path)
    abandoned = tmp_path / ".events.jsonl.abandoned.tmp"
    abandoned.write_text("partial", encoding="utf-8")
    valid_path = tmp_path / "valid-events.jsonl"
    append_event(valid_path, heartbeat(1))
    assert len(read_events(valid_path)) == 1
