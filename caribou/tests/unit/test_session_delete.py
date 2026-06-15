import asyncio
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Event

from caribou.server.models import SessionCreateRequest, SessionStatus
from caribou.server.session_manager import SessionManager, _Session


class FakeSandbox:
    def __init__(self):
        self.stop_calls = 0

    def stop_container(self):
        self.stop_calls += 1
        return True


def _make_session(session_id: str, output_dir: Path, sandbox=None) -> _Session:
    return _Session(
        id=session_id,
        config=SessionCreateRequest(
            mode="auto",
            run_mode="full_system",
            agent_system="caribou",
            llm_backend="chatgpt",
            sandbox_type="singularity",
            dataset_path="/tmp/example.h5ad",
            max_turns=1,
            initial_prompt="run",
        ),
        status=SessionStatus.idle,
        current_agent="agent",
        current_turn=0,
        messages=[],
        artifacts=[],
        code_events=[],
        output_dir=output_dir,
        events=[],
        event_condition=asyncio.Condition(),
        stop_flag=Event(),
        user_input_queue=Queue(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        sandbox_manager=sandbox,
    )


def _make_manager(tmp_path: Path, monkeypatch) -> SessionManager:
    monkeypatch.setattr("caribou.server.session_manager._SESSIONS_DIR", tmp_path)
    manager = object.__new__(SessionManager)
    manager._sessions = {}
    manager._deleted_session_ids = set()
    manager._lock = asyncio.Lock()
    return manager


def test_delete_session_removes_persisted_session_directory(tmp_path, monkeypatch):
    async def run_test():
        manager = _make_manager(tmp_path, monkeypatch)
        sandbox = FakeSandbox()
        session = _make_session("session-a", tmp_path / "session-a" / "outputs", sandbox)
        manager._sessions[session.id] = session
        manager._save_session(session)

        assert (tmp_path / "session-a" / "session.json").exists()

        await manager.delete_session(session.id)

        assert session.id not in manager._sessions
        assert session.stop_flag.is_set()
        assert sandbox.stop_calls == 1
        assert not (tmp_path / "session-a").exists()

    asyncio.run(run_test())


def test_late_events_after_delete_do_not_recreate_session_file(tmp_path, monkeypatch):
    async def run_test():
        manager = _make_manager(tmp_path, monkeypatch)
        session = _make_session("session-b", tmp_path / "session-b" / "outputs")
        manager._sessions[session.id] = session
        manager._save_session(session)

        await manager.delete_session(session.id)
        manager._on_event(session, {
            "type": "status_change",
            "session_id": session.id,
            "turn": 0,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {"status": "idle", "reason": "late_event"},
        })

        assert not (tmp_path / "session-b" / "session.json").exists()
        assert session.events == []

    asyncio.run(run_test())
