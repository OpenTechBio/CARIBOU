from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from caribou.execution.session_recovery import (
    capture_checkpoint,
    literal_replay,
    load_checkpoint,
)


class ReplaySandbox:
    def __init__(self) -> None:
        self.sources: list[str] = []

    def exec_code(self, source: str, timeout: int):
        self.sources.append(source)
        if source == "raise originally":
            return {"status": "error", "stdout": "", "stderr": "original failure"}
        return {"status": "ok", "stdout": "done", "stderr": ""}


def test_checkpoint_round_trips_runner_owned_action_ledger(tmp_path: Path) -> None:
    dataset = tmp_path / "input.h5ad"
    dataset.write_bytes(b"fixture")
    output_dir = tmp_path / "session" / "outputs"
    output_dir.mkdir(parents=True)
    session = SimpleNamespace(
        id="session-1",
        output_dir=output_dir,
        config=SimpleNamespace(dataset_path=str(dataset)),
        current_agent="analyst",
        current_turn=0,
        sandbox_manager=None,
        memory_manager=None,
        events=[],
        checkpoint_id=None,
        checkpoint_turn=None,
        checkpoint_healthy=False,
    )
    actions = [
        {
            "action_id": "session-1:1:1",
            "turn": 1,
            "agent_name": "analyst",
            "source": "raise originally",
            "recorded_result": {"success": False, "stderr": "original failure"},
        }
    ]

    captured = capture_checkpoint(
        session=session,
        history=[{"role": "system", "content": "policy"}],
        runner_state={
            "current_agent_name": "analyst",
            "turns_completed": 0,
            "action_ledger": actions,
        },
    )
    loaded = load_checkpoint(output_dir)

    assert loaded == captured
    assert loaded["actions"] == actions
    assert loaded["dataset"]["kind"] == "original_dataset"
    assert session.checkpoint_id == captured["checkpoint_id"]
    assert (output_dir.parent / ".checkpoints" / "latest.json").is_file()


def test_literal_replay_includes_originally_failed_attempts() -> None:
    sandbox = ReplaySandbox()
    progress: list[dict] = []
    checkpoint = {
        "actions": [
            {
                "source": "x = 1",
                "recorded_result": {"success": True},
            },
            {
                "source": "raise originally",
                "recorded_result": {"success": False},
            },
        ]
    }

    recovered, detail = literal_replay(
        sandbox=sandbox,
        checkpoint=checkpoint,
        emit=progress.append,
    )

    assert recovered is True
    assert "2 recorded code attempts" in detail
    assert sandbox.sources == ["x = 1", "raise originally"]
    assert [item["step"] for item in progress] == [1, 2]


def test_rolling_checkpoints_prune_superseded_unreferenced_versions(tmp_path: Path) -> None:
    dataset = tmp_path / "input.h5ad"
    dataset.write_bytes(b"fixture")
    output_dir = tmp_path / "session" / "outputs"
    output_dir.mkdir(parents=True)
    session = SimpleNamespace(
        id="session-rolling",
        output_dir=output_dir,
        config=SimpleNamespace(dataset_path=str(dataset)),
        current_agent="analyst",
        current_turn=0,
        sandbox_manager=None,
        memory_manager=None,
        events=[],
        attempts=[],
        forked_from_checkpoint_id=None,
        checkpoint_id=None,
        checkpoint_turn=None,
        checkpoint_healthy=False,
    )

    for _ in range(5):
        capture_checkpoint(
            session=session,
            history=[],
            runner_state={"current_agent_name": "analyst", "turns_completed": 0},
        )

    retained = list((output_dir.parent / ".checkpoints").glob("checkpoint_*/checkpoint.json"))
    assert len(retained) == 3
    assert load_checkpoint(output_dir)["checkpoint_id"] == session.checkpoint_id
