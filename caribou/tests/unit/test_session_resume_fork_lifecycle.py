from __future__ import annotations

import asyncio
import queue
import threading
from datetime import datetime
from pathlib import Path

from caribou.server.models import (
    RecoveryStatus,
    SessionCreateRequest,
    SessionForkRequest,
    SessionResumeRequest,
    SessionStatus,
)
from caribou.server.session_manager import SessionManager
from caribou.server.session_state import _Session
from caribou.core.python_environments import ResolvedPythonEnvironment


def _stopped_session(tmp_path: Path) -> _Session:
    config = SessionCreateRequest(
        name="source",
        mode="interactive",
        run_mode="full_system",
        agent_system="caribou",
        llm_backend="openrouter",
        model_name="provider/source-model",
        sandbox_type="singularity",
        dataset_path=str(tmp_path / "input.h5ad"),
    )
    return _Session(
        id="source-id",
        name="source",
        config=config,
        status=SessionStatus.stopped,
        current_agent="analyst",
        current_turn=4,
        messages=[],
        artifacts=[],
        code_events=[],
        output_dir=tmp_path / "source-id" / "outputs",
        events=[],
        event_condition=asyncio.Condition(),
        stop_flag=threading.Event(),
        cancel_response_flag=threading.Event(),
        user_input_queue=queue.Queue(),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        checkpoint_id="checkpoint-source",
        checkpoint_turn=4,
        checkpoint_healthy=True,
        attempts=[
            {
                "attempt_number": 1,
                "kind": "initial",
                "ended_at": datetime.utcnow().isoformat(),
            }
        ],
    )


def _manager(session: _Session) -> SessionManager:
    manager = object.__new__(SessionManager)
    manager._sessions = {session.id: session}
    manager._deleted_session_ids = set()
    manager._lock = asyncio.Lock()
    manager._save_session = lambda _session: None
    return manager


def test_resume_keeps_identity_and_creates_a_new_attempt(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        session = _stopped_session(tmp_path)
        session.python_environment = ResolvedPythonEnvironment(
            mode="host",
            path="/shared/envs/analysis",
            python_executable="/shared/envs/analysis/bin/python",
            kind="conda",
        )
        manager = _manager(session)
        completed = asyncio.Event()

        async def fake_recover(_session, _request):
            completed.set()

        monkeypatch.setattr(manager, "_recover_session", fake_recover)
        response = await manager.resume_session(
            session.id,
            SessionResumeRequest(recovery_mode="smart", target_mode="interactive"),
        )
        await asyncio.wait_for(completed.wait(), timeout=1)

        assert response.id == session.id
        assert session.status == SessionStatus.recovering
        assert session.recovery_status == RecoveryStatus.recovering
        assert session.attempt_number == 2
        assert session.attempts[-1]["kind"] == "resume"
        assert session.attempts[-1]["source_checkpoint_id"] == "checkpoint-source"
        assert response.python_environment.mode == "host"

    asyncio.run(run())


def test_fork_records_lineage_and_preserves_model_when_not_overridden(
    tmp_path: Path, monkeypatch
) -> None:
    async def run() -> None:
        session = _stopped_session(tmp_path)
        session.python_environment = ResolvedPythonEnvironment(
            mode="host",
            path="/shared/envs/analysis",
            python_executable="/shared/envs/analysis/bin/python",
            kind="conda",
        )
        manager = _manager(session)
        completed = asyncio.Event()

        async def fake_complete(_source, _child, _request):
            completed.set()

        monkeypatch.setattr(manager, "_complete_fork", fake_complete)
        monkeypatch.setattr("caribou.server.session_manager.SESSIONS_DIR", tmp_path)
        monkeypatch.setattr("caribou.server.session_manager._create_session_logger", lambda *_: None)

        response = await manager.fork_session(
            session.id,
            SessionForkRequest(
                name="named fork",
                recovery_mode="smart",
                target_mode="interactive",
            ),
        )
        await asyncio.wait_for(completed.wait(), timeout=1)
        child = manager.get_session(response.id)

        assert child is not None
        assert child.id != session.id
        assert child.name == "named fork"
        assert child.parent_session_id == session.id
        assert child.config.model_name == "provider/source-model"
        assert child.recovery_status == RecoveryStatus.awaiting_checkpoint
        assert child.attempts[-1]["kind"] == "fork"
        assert child.python_environment == session.python_environment

    asyncio.run(run())


def test_recovery_progress_and_system_messages_update_durable_session_state(
    tmp_path: Path,
) -> None:
    session = _stopped_session(tmp_path)
    manager = _manager(session)

    manager.append_event(
        session,
        {
            "type": "recovery_progress",
            "session_id": session.id,
            "turn": 4,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "phase": "starting_sandbox",
                "detail": "Starting a fresh Singularity sandbox.",
                "step": 4,
                "total_steps": 8,
                "substep": None,
                "substep_total": None,
            },
        },
    )
    system_event = {
        "type": "system_message",
        "session_id": session.id,
        "turn": 4,
        "timestamp": datetime.utcnow().isoformat(),
        "data": {"content": "Internal runner guidance", "category": "Runner guidance"},
    }
    manager.append_event(session, system_event)

    response = session.to_response()
    assert response.recovery_phase == "starting_sandbox"
    assert response.recovery_step == 4
    assert response.recovery_total_steps == 8
    assert session.messages[-1].role == "system"
    assert session.messages[-1].content == "Internal runner guidance"
    assert system_event["data"]["id"] == session.messages[-1].id
