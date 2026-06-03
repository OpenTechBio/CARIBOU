"""
SessionManager: owns all active CARIBOU sessions on the server.

Each session wraps an agent run: it holds the sandbox, LLM client, agent
system, event log, and the asyncio machinery to stream events to WebSocket
clients. Multiple WebSocket connections can observe the same session — they
all see full history on connect then live events from that point forward.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import textwrap
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv

from caribou.config import (
    CARIBOU_HOME,
    DEFAULT_AGENT_DIR,
    DEFAULT_BLUEPRINT_NAME,
    ENV_FILE,
)
from caribou.server.models import (
    ArtifactRecord,
    ArtifactType,
    CodeEventRecord,
    MessageRecord,
    SessionCreateRequest,
    SessionMode,
    SessionResponse,
    SessionStatus,
)

_UPLOADS_DIR = CARIBOU_HOME / "server_uploads"
_SESSIONS_DIR = CARIBOU_HOME / "server_sessions"
_SANDBOX_DATA_PATH = "/workspace/dataset.h5ad"
_SANDBOX_REF_DATA_PATH = "/workspace/reference.h5ad"


# ---------------------------------------------------------------------------
# Internal session record
# ---------------------------------------------------------------------------

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
    user_input_queue: queue.Queue
    created_at: datetime
    updated_at: datetime
    # Set once sandbox + runner are wired up
    sandbox_manager: Any = None
    llm_client: Any = None
    agent_system: Any = None
    driver_agent: Any = None
    initial_history: List[Dict] = field(default_factory=list)
    # Live history — mutated in-place by the runner so we always have the latest state
    live_history: List[Dict] = field(default_factory=list)
    model_name: str = ""
    analysis_context: str = ""
    runner_task: Optional[asyncio.Task] = None

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


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

# Events that don't need to be persisted mid-stream (too frequent)
_SKIP_PERSIST_TYPES = {"token", "pong"}


class SessionManager:

    def __init__(self) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        self._load_persisted_sessions()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _session_file(self, session_id: str) -> Path:
        return _SESSIONS_DIR / session_id / "session.json"

    def _save_session(self, session: _Session) -> None:
        """Write session state to disk. Called after every non-token event."""
        try:
            path = self._session_file(session.id)
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
            path.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass  # persistence failure must never crash the server

    def _load_persisted_sessions(self) -> None:
        """On startup, reload all sessions saved to disk."""
        if not _SESSIONS_DIR.exists():
            return
        for session_dir in sorted(_SESSIONS_DIR.iterdir()):
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue
            try:
                data = json.loads(session_file.read_text())
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
                    output_dir=session_dir / "outputs",
                    events=data.get("events", []),
                    event_condition=asyncio.Condition(),
                    stop_flag=threading.Event(),
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
                self._sessions[session.id] = session
            except Exception:
                pass  # skip corrupt session files

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(self, config: SessionCreateRequest) -> SessionResponse:
        session_id = str(uuid4())
        output_dir = _SESSIONS_DIR / session_id / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)

        session = _Session(
            id=session_id,
            config=config,
            status=SessionStatus.initializing,
            current_agent="",
            current_turn=0,
            messages=[],
            artifacts=[],
            code_events=[],
            output_dir=output_dir,
            events=[],
            event_condition=asyncio.Condition(),
            stop_flag=threading.Event(),
            user_input_queue=queue.Queue(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        async with self._lock:
            self._sessions[session_id] = session

        self._save_session(session)
        asyncio.create_task(self._initialize_session(session))
        return session.to_response()

    def get_session(self, session_id: str) -> Optional[_Session]:
        return self._sessions.get(session_id)

    def list_sessions(self) -> List[SessionResponse]:
        return [s.to_response() for s in self._sessions.values()]

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session:
            session.stop_flag.set()

    async def delete_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.stop_flag.set()
            if session.runner_task and not session.runner_task.done():
                session.runner_task.cancel()
            if session.sandbox_manager:
                try:
                    await asyncio.to_thread(session.sandbox_manager.stop_container)
                except Exception:
                    pass

    async def send_user_message(self, session_id: str, content: str) -> bool:
        """Put a user message into the interactive-mode queue. Returns False if not found."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.user_input_queue.put(content)
        return True

    async def start_run(self, session_id: str, initial_prompt: str) -> bool:
        """
        Called when the WebSocket client sends a 'run' message.
        Launches the runner task from the beginning.
        """
        session = self._sessions.get(session_id)
        if not session or session.status not in (SessionStatus.idle,):
            return False
        if session.runner_task and not session.runner_task.done():
            return False

        history = list(session.initial_history)
        history.append({"role": "user", "content": initial_prompt})
        # Store on session so extend_run can resume from here
        session.live_history = history

        return await self._launch_runner(session, history)

    async def extend_run(self, session_id: str, additional_turns: int) -> bool:
        """
        Resume a stopped auto session for additional_turns more turns,
        continuing from the exact conversation state where it left off.
        """
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.status not in (SessionStatus.stopped,):
            return False
        if session.config.mode != SessionMode.auto:
            return False
        if not session.live_history:
            return False
        if not session.sandbox_manager or not session.agent_system:
            return False
        if session.runner_task and not session.runner_task.done():
            return False

        # Reset stop flag for the new run
        session.stop_flag.clear()
        session.status = SessionStatus.idle

        # Update max_turns to allow additional_turns more from current position
        new_max = session.current_turn + additional_turns
        session.config = session.config.model_copy(update={"max_turns": new_max})

        self._on_event(session, {
            "type": "status_change",
            "session_id": session.id,
            "turn": session.current_turn,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {"status": "running", "reason": f"extended by {additional_turns} turns"},
        })

        return await self._launch_runner(session, session.live_history)

    async def _launch_runner(self, session: _Session, history: List[Dict]) -> bool:
        """Shared runner launch logic used by start_run and extend_run."""
        from caribou.server.streaming_runner import run_session_async

        is_auto = session.config.mode == SessionMode.auto
        max_turns = session.config.max_turns or 20

        session.runner_task = asyncio.create_task(
            run_session_async(
                session_id=session.id,
                agent_system=session.agent_system,
                driver_agent=session.driver_agent,
                analysis_context=session.analysis_context,
                llm_client=session.llm_client,
                sandbox_manager=session.sandbox_manager,
                history=history,
                is_auto=is_auto,
                max_turns=max_turns,
                model_name=session.model_name,
                output_dir=session.output_dir,
                event_callback=lambda ev: self._on_event(session, ev),
                stop_flag=session.stop_flag,
                user_input_queue=session.user_input_queue if not is_auto else None,
            )
        )
        return True

    def append_event(self, session: _Session, event: Dict[str, Any]) -> None:
        """Synchronously append event and schedule condition notification."""
        session.events.append(event)
        session.updated_at = datetime.utcnow()
        self._process_event(session, event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_event(self, session: _Session, event: Dict[str, Any]) -> None:
        """Called from run_session_async (main thread via call_soon_threadsafe)."""
        session.events.append(event)
        session.updated_at = datetime.utcnow()
        self._process_event(session, event)
        if event.get("type") not in _SKIP_PERSIST_TYPES:
            self._save_session(session)
        asyncio.ensure_future(self._notify_condition(session))

    async def _notify_condition(self, session: _Session) -> None:
        async with session.event_condition:
            session.event_condition.notify_all()

    def _process_event(self, session: _Session, event: Dict[str, Any]) -> None:
        """Update session state from incoming events."""
        t = event.get("type")
        data = event.get("data", {})

        if t == "status_change":
            status_str = data.get("status", "")
            try:
                session.status = SessionStatus(status_str)
            except ValueError:
                pass

        elif t == "message_complete":
            msg_data = data.get("message", {})
            session.current_turn = msg_data.get("turn", session.current_turn)
            session.current_agent = msg_data.get("agent_name", session.current_agent)
            session.messages.append(MessageRecord(
                id=msg_data.get("id", str(uuid4())),
                session_id=session.id,
                turn=msg_data.get("turn", 0),
                role=msg_data.get("role", "assistant"),
                agent_name=msg_data.get("agent_name", ""),
                content=msg_data.get("content", ""),
                timestamp=datetime.utcnow(),
            ))

        elif t == "agent_switch":
            session.current_agent = data.get("to_agent", session.current_agent)

        elif t == "code_result":
            session.code_events.append(CodeEventRecord(
                session_id=session.id,
                turn=event.get("turn", 0),
                agent_name=data.get("agent_name", ""),
                source="",  # source is in the preceding code_submitted event
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
                success=data.get("success", True),
                duration_ms=data.get("duration_ms", 0),
            ))

        elif t == "artifact":
            art = data.get("artifact", {})
            art_type_str = art.get("type", "data")
            try:
                art_type = ArtifactType(art_type_str)
            except ValueError:
                art_type = ArtifactType.data
            record = ArtifactRecord(
                session_id=session.id,
                turn=art.get("turn", event.get("turn", 0)),
                type=art_type,
                filename=art.get("filename", ""),
                mime_type=art.get("mime_type", "application/octet-stream"),
                size_bytes=art.get("size_bytes", 0),
                local_path=art.get("local_path", ""),
            )
            session.artifacts.append(record)

    async def _initialize_session(self, session: _Session) -> None:
        """
        Runs in background after session creation.
        Sets up sandbox, LLM client, agent system, and emits status events.
        """
        load_dotenv(dotenv_path=ENV_FILE, override=True)

        def _emit_init(event_type: str, data: Dict) -> None:
            self._on_event(session, {
                "type": event_type,
                "session_id": session.id,
                "turn": 0,
                "timestamp": datetime.utcnow().isoformat(),
                "data": data,
            })

        try:
            # --- Agent system ---
            from caribou.agents.AgentSystem import AgentSystem
            blueprint_path = _find_blueprint(session.config.agent_system)
            agent_sys = AgentSystem.load_from_json(str(blueprint_path))
            session.agent_system = agent_sys

            # Pick driver agent: first agent in the system
            driver_name = next(iter(agent_sys.agents))
            session.driver_agent = agent_sys.get_agent(driver_name)
            session.current_agent = driver_name

            # --- LLM client ---
            llm_client, model_name = _build_llm_client(session.config.llm_backend)
            session.llm_client = llm_client
            session.model_name = model_name

            # --- Analysis context + initial history ---
            analysis_context = textwrap.dedent(f"""\
                Primary dataset path: **{_SANDBOX_DATA_PATH}**
                {"Reference dataset path: **" + _SANDBOX_REF_DATA_PATH + "**" if session.config.reference_dataset_path else ""}

                **IMPORTANT**: Please save all generated output files (plots, .h5ad, .csv) to the /workspace/outputs/ directory.
            """).strip()
            session.analysis_context = analysis_context

            driver = session.driver_agent
            system_prompt = driver.get_full_prompt(None) + "\n\n" + analysis_context
            session.initial_history = [
                {"role": "system", "content": f"**GLOBAL POLICY**: {agent_sys.global_policy}\n"},
                {"role": "system", "content": system_prompt},
            ]

            # --- Sandbox ---
            _emit_init("status_change", {"status": "initializing", "reason": "starting_sandbox"})
            sandbox_manager = await asyncio.to_thread(
                _build_sandbox, session.config, session.output_dir
            )
            session.sandbox_manager = sandbox_manager

            session.status = SessionStatus.idle
            _emit_init("status_change", {"status": "idle", "reason": "ready"})

            # Auto sessions with a prompt start immediately — no WebSocket run message needed
            if session.config.mode == SessionMode.auto and session.config.initial_prompt:
                await self.start_run(session.id, session.config.initial_prompt)

        except Exception as exc:
            session.status = SessionStatus.error
            _emit_init("error", {"code": "INIT_ERROR", "message": str(exc), "fatal": True})
            _emit_init("status_change", {"status": "error", "reason": str(exc)})


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _find_blueprint(name: str) -> Path:
    """Resolve a blueprint name to a JSON path."""
    from caribou.config import DEFAULT_AGENT_DIR
    from caribou.cli.run_cli import PACKAGE_AGENTS_DIR

    for search_dir in (DEFAULT_AGENT_DIR, PACKAGE_AGENTS_DIR):
        candidate = Path(search_dir) / name
        if candidate.exists():
            return candidate
        candidate = Path(search_dir) / f"{name}.json"
        if candidate.exists():
            return candidate

    # Absolute path provided
    p = Path(name)
    if p.exists():
        return p

    raise FileNotFoundError(f"Blueprint '{name}' not found in {DEFAULT_AGENT_DIR} or package agents dir.")


def _build_llm_client(backend: str):
    """Return (llm_client, model_name) for the given backend string."""
    from openai import OpenAI

    if backend == "chatgpt":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise EnvironmentError("OPENAI_API_KEY not set.")
        return OpenAI(api_key=key), "gpt-4o"

    if backend == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set.")
        from caribou.core.anthropic_wrapper import AnthropicClient
        return AnthropicClient(api_key=key), "claude-sonnet-4-6"

    if backend == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise EnvironmentError("DEEPSEEK_API_KEY not set.")
        return OpenAI(api_key=key, base_url="https://api.deepseek.com"), "deepseek-chat"

    if backend.startswith("ollama"):
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        from caribou.core.ollama_wrapper import OllamaClient
        return OllamaClient(host=host), "llama3"

    raise ValueError(f"Unknown LLM backend: {backend!r}")


def _build_sandbox(config: SessionCreateRequest, output_dir: Path):
    """Build and start a sandbox manager. Blocking — run in a thread."""
    from pathlib import Path as _Path
    from rich.console import Console
    from caribou.core.sandbox_management import init_docker, init_singularity_exec

    script_dir = _Path(__file__).resolve().parent
    # Quiet console so sandbox init output doesn't go to stdout;
    # errors are surfaced via exceptions caught by _initialize_session.
    console = Console(quiet=True)

    if config.sandbox_type.value == "docker":
        manager_class, handle, copy_cmd, _, _ = init_docker(
            script_dir, subprocess, console, force_refresh=False
        )
        sandbox = manager_class()
        if not sandbox.start_container():
            raise RuntimeError("Docker sandbox failed to start.")
        copy_cmd(config.dataset_path, f"{handle}:{_SANDBOX_DATA_PATH}")
        if config.reference_dataset_path:
            copy_cmd(config.reference_dataset_path, f"{handle}:{_SANDBOX_REF_DATA_PATH}")
        return sandbox

    if config.sandbox_type.value == "singularity":
        manager_class, _, _, _, _ = init_singularity_exec(
            script_dir, _SANDBOX_DATA_PATH, subprocess, console, force_refresh=False
        )
        sandbox = manager_class()
        sandbox.set_data(
            [(Path(config.dataset_path), _SANDBOX_DATA_PATH)]
            + ([(Path(config.reference_dataset_path), _SANDBOX_REF_DATA_PATH)] if config.reference_dataset_path else []),
            output_dir,
        )
        if not sandbox.start_container():
            raise RuntimeError("Singularity sandbox failed to start.")
        return sandbox

    raise ValueError(f"Unknown sandbox type: {config.sandbox_type}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

session_manager = SessionManager()
