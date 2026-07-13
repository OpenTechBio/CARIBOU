"""
Disk persistence for CARIBOU sessions.

Every non-token event triggers a session.json write so a crash or restart
leaves us with an up-to-date snapshot to reload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

from caribou.server.models import (
    ArtifactRecord,
    CodeEventRecord,
    MessageRecord,
    SessionCreateRequest,
    SessionStatus,
)
from caribou.server.session_state import SESSIONS_DIR, _Session

_log = logging.getLogger(__name__)


def session_file(session_id: str, sessions_dir: Path = SESSIONS_DIR) -> Path:
    return sessions_dir / session_id / "session.json"


def session_dir(session_id: str, sessions_dir: Path = SESSIONS_DIR) -> Path:
    return sessions_dir / session_id


def save_session(
    session: _Session,
    is_deleted: Callable[[str], bool],
    sessions_dir: Path = SESSIONS_DIR,
) -> None:
    """
    Write session state to disk. Called after every non-token event.

    `is_deleted` is passed in (rather than checked once) so we can bail out
    if the session gets deleted between the check and the write.
    """
    if is_deleted(session.id):
        return
    try:
        path = session_file(session.id, sessions_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "id": session.id,
            "config": session.config.model_dump(),
            "status": session.status.value,
            "current_agent": session.current_agent,
            "current_turn": session.current_turn,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "messages": [m.model_dump() for m in session.messages],
            "artifacts": [a.model_dump() for a in session.artifacts],
            "code_events": [c.model_dump() for c in session.code_events],
            "events": session.events,
        }
        if is_deleted(session.id):
            return
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as exc:
        # Persistence failure must never crash the server, but do log it.
        _log.warning("Failed to persist session %s: %s", session.id, exc)


def load_persisted_sessions(sessions_dir: Path = SESSIONS_DIR) -> Dict[str, _Session]:
    """On startup, reload all sessions saved to disk."""
    sessions: Dict[str, _Session] = {}
    if not sessions_dir.exists():
        return sessions
    for sess_dir in sorted(sessions_dir.iterdir()):
        f = sess_dir / "session.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            config = SessionCreateRequest(**data["config"])
            raw_status = data.get("status", "stopped")
            # Sessions that were mid-run when the server died are now stopped
            if raw_status in ("running", "initializing"):
                raw_status = "stopped"
            status = SessionStatus(raw_status)

            session = _Session(
                id=data["id"],
                config=config,
                status=status,
                current_agent=data.get("current_agent", ""),
                current_turn=data.get("current_turn", 0),
                messages=[MessageRecord(**m) for m in data.get("messages", [])],
                artifacts=[ArtifactRecord(**a) for a in data.get("artifacts", [])],
                code_events=[CodeEventRecord(**c) for c in data.get("code_events", [])],
                output_dir=sess_dir / "outputs",
                events=data.get("events", []),
                event_condition=asyncio.Condition(),
                stop_flag=threading.Event(),
                cancel_response_flag=threading.Event(),
                user_input_queue=queue.Queue(),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
            # If the session was interrupted, record that in the event log
            if raw_status != data.get("status"):
                session.events.append({
                    "type": "status_change",
                    "session_id": session.id,
                    "turn": session.current_turn,
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": {"status": "stopped", "reason": "server restarted"},
                })
            sessions[session.id] = session
        except Exception as exc:
            # Skip but log — silent skips have masked schema drift and
            # disk corruption in prior incidents.
            _log.warning("Skipping corrupt session file %s: %s", f, exc)
    return sessions
