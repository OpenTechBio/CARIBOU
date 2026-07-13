"""
Session data types and shared constants for the CARIBOU server.

Kept separate from `session_manager` so persistence, setup, and routing
code can import the record type without pulling in the whole manager.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from caribou.config import CARIBOU_HOME
from caribou.server.models import (
    ArtifactRecord,
    CodeEventRecord,
    MessageRecord,
    SessionCreateRequest,
    SessionResponse,
    SessionStatus,
)

# Filesystem layout
UPLOADS_DIR = CARIBOU_HOME / "server_uploads"
SESSIONS_DIR = CARIBOU_HOME / "server_sessions"

# Sandbox paths mounted inside every session's container
SANDBOX_DATA_PATH = "/workspace/dataset.h5ad"
SANDBOX_REF_DATA_PATH = "/workspace/reference.h5ad"

# Events that don't need to be persisted mid-stream (too frequent)
SKIP_PERSIST_TYPES = {"token", "pong"}

# Bound the in-memory event log per session so long-running sessions don't
# leak memory as tokens/messages accumulate. When exceeded, oldest events
# are dropped; the persisted session.json still reflects the latest state.
MAX_EVENTS_PER_SESSION = 5000
# When trimming kicks in, keep this many recent events.
EVENTS_TRIM_TARGET = 4000


def trim_events(events: List[Dict[str, Any]]) -> None:
    """Trim the events list in place if it exceeds the max."""
    if len(events) > MAX_EVENTS_PER_SESSION:
        drop = len(events) - EVENTS_TRIM_TARGET
        del events[:drop]


@dataclass
class _Session:
    id: str
    config: SessionCreateRequest
    status: SessionStatus
    current_agent: str
    current_turn: int
    messages: List[MessageRecord]
    artifacts: List[ArtifactRecord]
    code_events: List[CodeEventRecord]
    output_dir: Path
    # Event log: every event emitted is appended here
    events: List[Dict[str, Any]]
    # Notified whenever a new event is appended
    event_condition: asyncio.Condition
    stop_flag: threading.Event
    cancel_response_flag: threading.Event
    user_input_queue: queue.Queue
    created_at: datetime
    updated_at: datetime
    # Set once sandbox + runner are wired up
    sandbox_manager: Any = None
    llm_client: Any = None
    agent_system: Any = None
    driver_agent: Any = None
    initial_history: List[Dict] = field(default_factory=list)
    model_name: str = ""
    analysis_context: str = ""
    runner_task: Optional[asyncio.Task] = None
    logger: Any = None

    def to_response(self) -> SessionResponse:
        return SessionResponse(
            id=self.id,
            status=self.status,
            mode=self.config.mode,
            run_mode=self.config.run_mode,
            agent_system=self.config.agent_system,
            llm_backend=self.config.llm_backend,
            sandbox_type=self.config.sandbox_type,
            dataset_path=self.config.dataset_path,
            max_turns=self.config.max_turns,
            current_turn=self.current_turn,
            current_agent=self.current_agent,
            created_at=self.created_at,
            updated_at=self.updated_at,
            artifact_count=len(self.artifacts),
            message_count=len(self.messages),
        )
