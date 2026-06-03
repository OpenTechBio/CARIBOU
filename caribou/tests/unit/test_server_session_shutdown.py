import asyncio
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Event

from caribou.server.models import SessionCreateRequest, SessionStatus
from caribou.server.session_manager import SessionManager, _Session


class FakeSandbox:
    def __init__(self, fail=False):
        self.fail = fail
        self.stop_calls = 0

    def stop_container(self):
        self.stop_calls += 1
        if self.fail:
            raise RuntimeError("stop failed")
        return True


def _make_session(session_id: str, status: SessionStatus, sandbox=None) -> _Session:
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
        status=status,
        current_agent="agent",
        current_turn=2,
        messages=[],
        artifacts=[],
        code_events=[],
        output_dir=Path("/tmp") / session_id,
        events=[],
        event_condition=asyncio.Condition(),
        stop_flag=Event(),
        user_input_queue=Queue(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        sandbox_manager=sandbox,
    )


def test_shutdown_all_stops_sandboxes_and_persists_stopped_sessions():
    async def run_test():
        manager = object.__new__(SessionManager)
        manager._sessions = {}
        manager._lock = asyncio.Lock()
        saved = []
        manager._save_session = lambda session: saved.append((session.id, session.status))

        sandbox_ok = FakeSandbox()
        sandbox_fail = FakeSandbox(fail=True)
        running = _make_session("running", SessionStatus.running, sandbox_ok)
        idle = _make_session("idle", SessionStatus.idle, sandbox_fail)
        stopped = _make_session("stopped", SessionStatus.stopped, None)
        manager._sessions = {
            running.id: running,
            idle.id: idle,
            stopped.id: stopped,
        }

        errors = await manager.shutdown_all()

        assert running.stop_flag.is_set()
        assert idle.stop_flag.is_set()
        assert stopped.stop_flag.is_set()
        assert sandbox_ok.stop_calls == 1
        assert sandbox_fail.stop_calls == 1
        assert running.status == SessionStatus.stopped
        assert idle.status == SessionStatus.stopped
        assert stopped.status == SessionStatus.stopped
        assert ("running", SessionStatus.stopped) in saved
        assert ("idle", SessionStatus.stopped) in saved
        assert ("stopped", SessionStatus.stopped) not in saved
        assert len(errors) == 1
        assert "sandbox shutdown failed" in errors[0]

    asyncio.run(run_test())
