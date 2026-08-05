"""
SessionManager: owns all active CARIBOU sessions on the server.

Each session wraps an agent run: it holds the sandbox, LLM client, agent
system, event log, and the asyncio machinery to stream events to WebSocket
clients. Multiple WebSocket connections can observe the same session — they
all see full history on connect then live events from that point forward.

Persistence, session-record shape, and bootstrap helpers live in sibling
modules:
  - session_state.py       — the `_Session` dataclass and constants
  - session_persistence.py — save/load session state to/from disk
  - session_setup.py       — blueprint / LLM / sandbox construction
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import textwrap
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv

from caribou.config import ENV_FILE
from caribou.execution.evaluation import (
    build_evaluation_payload,
    resolve_evaluator_agent,
    run_evaluation,
)
from caribou.execution.token_utils import estimate_tokens
from caribou.server.models import (
    ArtifactRecord,
    ArtifactType,
    CodeEventRecord,
    EvaluationResult,
    MemoryStrategy,
    MessageRecord,
    SessionCreateRequest,
    SessionMode,
    SessionResponse,
    SessionStatus,
)
from caribou.server.session_persistence import (
    load_persisted_sessions,
    save_session,
    session_dir,
)
from caribou.server.session_setup import (
    build_llm_client,
    build_sandbox,
    find_blueprint,
    resolve_model_info,
)
from caribou.server.session_state import (
    SANDBOX_DATA_PATH,
    SANDBOX_REF_DATA_PATH,
    SESSIONS_DIR,
    SKIP_PERSIST_TYPES,
    _Session,
    trim_events,
)

# Backwards-compatible re-exports for callers that reach into this module.
_SESSIONS_DIR = SESSIONS_DIR


def _create_session_logger(session_id: str, session_dir_path) -> logging.Logger:
    """Create a logger isolated to one session's stderr and session.log."""
    short = session_id[:8]
    logger = logging.getLogger(f"caribou.session.{short}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    log_file = session_dir_path / "session.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt=f"%(asctime)s.%(msecs)03d  [{short}]  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter(
        fmt=f"%(asctime)s  [session {short}]  %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(stream_handler)
    return logger


def _close_session_logger(logger: logging.Logger) -> None:
    """Flush and close every handler owned by a per-session logger."""
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)


class SessionManager:

    def __init__(self) -> None:
        self._deleted_session_ids: set[str] = set()
        self._lock = asyncio.Lock()
        self._sessions: Dict[str, _Session] = load_persisted_sessions()

    # ------------------------------------------------------------------
    # Persistence adapters (keep the manager as the single call site)
    # ------------------------------------------------------------------

    def _is_deleted(self, session_id: str) -> bool:
        return session_id in getattr(self, "_deleted_session_ids", set())

    def _save_session(self, session: _Session) -> None:
        save_session(session, self._is_deleted, _SESSIONS_DIR)

    def _session_dir(self, session_id: str):
        return session_dir(session_id, _SESSIONS_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(self, config: SessionCreateRequest) -> SessionResponse:
        import queue
        import threading

        session_id = str(uuid4())
        output_dir = SESSIONS_DIR / session_id / "outputs"
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
            cancel_response_flag=threading.Event(),
            user_input_queue=queue.Queue(),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            resolved_model=resolve_model_info(config),
        )

        async with self._lock:
            self._deleted_session_ids.discard(session_id)
            self._sessions[session_id] = session

        session.logger = _create_session_logger(session_id, output_dir.parent)
        session.logger.info(
            "Session created | backend: %s | model: %s | mode: %s | sandbox: %s | log: %s",
            config.llm_backend,
            (
                session.resolved_model.model
                if session.resolved_model is not None
                else "unresolved"
            ),
            config.mode.value,
            config.sandbox_type.value,
            output_dir.parent / "session.log",
        )
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

    async def cancel_response(self, session_id: str) -> bool:
        """Cancel an in-flight interactive response without ending the session."""
        session = self._sessions.get(session_id)
        if (
            not session
            or session.config.mode != SessionMode.interactive
            or session.status != SessionStatus.running
            or not session.runner_task
            or session.runner_task.done()
        ):
            return False
        session.cancel_response_flag.set()
        return True

    def get_memory_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return a read-only snapshot of the session's memory state."""
        session = self._sessions.get(session_id)
        if not session or not session.memory_manager:
            return None
        return session.memory_manager.get_state()

    def get_context_breakdown(self, session_id: str) -> Dict[str, Any]:
        """Return the context breakdown for any session."""
        session = self._sessions.get(session_id)
        if not session:
            return {"error": "Session not found"}

        if session.memory_manager is not None:
            return session.memory_manager.get_state()

        # Fallback: compute breakdown from the pinned system messages set up at
        # session init plus the raw per-turn message records.
        pinned_messages = session.initial_history
        pinned_tokens = sum(estimate_tokens(m.get("content", "")) for m in pinned_messages)

        user_count = assistant_count = system_count = 0
        user_tokens = assistant_tokens = system_tokens = 0
        for msg in session.messages:
            tokens = estimate_tokens(msg.content)
            if msg.role == "user":
                user_count += 1
                user_tokens += tokens
            elif msg.role == "assistant":
                assistant_count += 1
                assistant_tokens += tokens
            else:
                system_count += 1
                system_tokens += tokens

        working_tokens = user_tokens + assistant_tokens + system_tokens
        total_messages = len(pinned_messages) + len(session.messages)
        total_tokens = pinned_tokens + working_tokens

        return {
            "strategy": session.config.memory_strategy.value,
            "total_messages": total_messages,
            "context_breakdown": {
                "pinned_system": len(pinned_messages),
                "pivotal_code": 0,
                "summaries": 0,
                "working_user": user_count,
                "working_assistant": assistant_count,
                "working_system": system_count,
                "total": total_messages,
                "total_full_history": total_messages,
                "pinned_system_tokens": pinned_tokens,
                "pivotal_code_tokens": 0,
                "summaries_tokens": 0,
                "working_user_tokens": user_tokens,
                "working_assistant_tokens": assistant_tokens,
                "working_system_tokens": system_tokens,
                "total_tokens": total_tokens,
                "total_full_history_tokens": total_tokens,
            },
        }

    async def evaluate_session(self, session_id: str) -> EvaluationResult:
        """Send this session's full transcript to an evaluator agent and
        return its assessment. Shares its resolution/call logic with the
        CLI's /evaluate command (execution/user_commands.py) via
        caribou.execution.evaluation — callers must have already confirmed
        the session exists and has been started (llm_client/agent_system set).
        """
        session = self._sessions[session_id]

        evaluator_agent, source = resolve_evaluator_agent(session.agent_system)

        history = list(session.initial_history) + [
            {"role": m.role, "content": m.content} for m in session.messages
        ]
        payload = build_evaluation_payload(
            run_id=session.id,
            turn=session.current_turn,
            active_agent=session.current_agent,
            history=history,
        )

        # The LLM call blocks; run it off the event loop like every other
        # provider call in this module (see build_llm_client's callers).
        assessment = await asyncio.to_thread(
            run_evaluation,
            evaluator_agent=evaluator_agent,
            llm_client=session.llm_client,
            model_name=session.model_name,
            payload=payload,
        )

        result = EvaluationResult(
            session_id=session.id,
            turn=session.current_turn,
            evaluator_agent=evaluator_agent.name,
            evaluator_source=source,
            model=session.model_name,
            assessment=assessment,
        )

        report_dir = session.output_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            report_dir / f"evaluation_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
        )
        report_path.write_text(result.model_dump_json(indent=2))

        return result

    async def delete_session(self, session_id: str) -> None:
        async with self._lock:
            self._deleted_session_ids.add(session_id)
            session = self._sessions.pop(session_id, None)
        if session:
            session.stop_flag.set()
            if session.runner_task and not session.runner_task.done():
                session.runner_task.cancel()
            if session.logger:
                session.logger.info("Session deleted — closing log")
                _close_session_logger(session.logger)
            if session.sandbox_manager:
                try:
                    await asyncio.to_thread(session.sandbox_manager.stop_container)
                except Exception:
                    pass
        try:
            await asyncio.to_thread(shutil.rmtree, self._session_dir(session_id), ignore_errors=True)
        except Exception:
            pass

    async def shutdown_all(self) -> List[str]:
        """
        Stop all active server-owned sessions and sandbox processes.

        This is used by the FastAPI lifespan shutdown hook. It is deliberately
        best-effort: one broken sandbox must not prevent the rest from being
        torn down.
        """
        errors: List[str] = []

        async with self._lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            session.stop_flag.set()

        for session in sessions:
            if session.runner_task and not session.runner_task.done():
                session.runner_task.cancel()

        for session in sessions:
            if session.runner_task and not session.runner_task.done():
                try:
                    await asyncio.wait_for(session.runner_task, timeout=5)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    errors.append(f"{session.id}: runner did not stop before timeout")
                except Exception as exc:
                    errors.append(f"{session.id}: runner shutdown failed: {exc}")

            if session.sandbox_manager:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(session.sandbox_manager.stop_container),
                        timeout=10,
                    )
                except asyncio.TimeoutError:
                    errors.append(f"{session.id}: sandbox did not stop before timeout")
                except Exception as exc:
                    errors.append(f"{session.id}: sandbox shutdown failed: {exc}")

            async with self._lock:
                if session.status in (SessionStatus.initializing, SessionStatus.running, SessionStatus.idle):
                    session.status = SessionStatus.stopped
                    session.updated_at = datetime.utcnow()
                    session.events.append({
                        "type": "status_change",
                        "session_id": session.id,
                        "turn": session.current_turn,
                        "timestamp": session.updated_at.isoformat(),
                        "data": {"status": "stopped", "reason": "server shutdown"},
                    })
                    trim_events(session.events)
                    self._save_session(session)

            if session.logger:
                session.logger.info("Session stopped during server shutdown — closing log")
                _close_session_logger(session.logger)

        return errors

    async def send_user_message(self, session_id: str, content: str) -> bool:
        """Queue one message only while a live interactive runner is waiting."""
        session = self._sessions.get(session_id)
        if (
            not session
            or session.config.mode != SessionMode.interactive
            or session.status != SessionStatus.idle
            or not session.runner_task
            or session.runner_task.done()
        ):
            return False
        self._on_event(session, {
            "type": "status_change",
            "session_id": session.id,
            "turn": session.current_turn,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {"status": "running", "reason": "user_message_queued"},
        })
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
        session.status = SessionStatus.running

        self._on_event(session, {
            "type": "message_complete",
            "session_id": session.id,
            "turn": 1,
            "timestamp": datetime.utcnow().isoformat(),
            "data": {
                "message": {
                    "id": f"msg_{session.id}_user_0",
                    "turn": 1,
                    "role": "user",
                    "agent_name": "",
                    "content": initial_prompt,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            },
        })

        return await self._launch_runner(session, history)

    async def _launch_runner(self, session: _Session, history: List[Dict]) -> bool:
        """Launch a session runner."""
        from caribou.server.streaming_runner import run_session_async

        is_auto = session.config.mode == SessionMode.auto
        max_turns = session.config.max_turns or 20

        # Determine effective memory strategy: new fields take precedence,
        # legacy booleans are the fallback.
        memory_strategy = session.config.memory_strategy
        if memory_strategy == MemoryStrategy.full:
            if session.config.agent_report_memory:
                memory_strategy = MemoryStrategy.agent_report
            elif session.config.compress_memory:
                memory_strategy = MemoryStrategy.episodic
        # Persist the effective strategy so to_response()/get_context_breakdown()
        # report what's actually running, even when it was derived from the
        # legacy compress_memory/agent_report_memory flags.
        session.config.memory_strategy = memory_strategy

        memory_manager = None
        report_memory = None

        if memory_strategy == MemoryStrategy.episodic:
            from caribou.execution.MemoryManager import MemoryManager
            whs = session.config.memory_working_history_size or 4
            st = session.config.memory_summarization_threshold or 20
            cs = session.config.memory_chunk_size or 10
            memory_manager = MemoryManager(
                llm_client=session.llm_client,
                model_name=session.model_name,
                initial_history=history,
                working_history_size=whs,
                summarization_threshold=st,
                chunk_size_to_summarize=cs,
            )
            session.memory_manager = memory_manager
            if session.logger:
                session.logger.info(
                    "Episodic memory enabled | working_history: %s | threshold: %s | chunk: %s",
                    whs, st, cs,
                )
            # agent_report and episodic are mutually exclusive
            if session.config.agent_report_memory:
                session.config.agent_report_memory = False

        elif memory_strategy == MemoryStrategy.agent_report:
            from caribou.execution.report_generation import AgentReportMemory
            base_globals = [history[0]] if history else []
            agent_prompt_content = history[1]["content"] if len(history) > 1 else ""
            report_memory = AgentReportMemory(base_globals, agent_prompt_content)
            session.memory_manager = report_memory
            if session.logger:
                session.logger.info("Agent report memory enabled")

        if session.logger:
            session.logger.info(
                "Runner launching | mode: %s | max_turns: %s | history_messages: %s | memory: %s",
                session.config.mode.value,
                max_turns,
                len(history),
                memory_strategy.value,
            )

        async def _guarded_runner() -> None:
            # Ensures the sandbox is torn down and the stop flag reset even if the
            # runner task is cancelled mid-turn (e.g., session deleted, server
            # shutdown). Without this, cancellations leak sandbox containers.
            try:
                await run_session_async(
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
                    cancel_response_flag=session.cancel_response_flag,
                    user_input_queue=session.user_input_queue if not is_auto else None,
                    logger=session.logger,
                    memory_manager=memory_manager,
                    report_memory=report_memory,
                )
            except asyncio.CancelledError:
                # Propagate after cleanup so shutdown_all/delete_session can await it.
                raise
            finally:
                # Best-effort sandbox shutdown for cancellation paths that don't
                # go through delete_session (e.g., server SIGTERM race).
                if self._is_deleted(session.id) and session.sandbox_manager is not None:
                    try:
                        await asyncio.to_thread(session.sandbox_manager.stop_container)
                    except Exception:
                        pass

        session.runner_task = asyncio.create_task(_guarded_runner())
        return True

    def append_event(self, session: _Session, event: Dict[str, Any]) -> None:
        """Synchronously append event and update derived state."""
        session.events.append(event)
        trim_events(session.events)
        session.updated_at = datetime.utcnow()
        self._process_event(session, event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_event(self, session: _Session, event: Dict[str, Any]) -> None:
        """Called from run_session_async (main thread via call_soon_threadsafe)."""
        if self._is_deleted(session.id):
            return
        session.events.append(event)
        trim_events(session.events)
        session.updated_at = datetime.utcnow()
        self._process_event(session, event)
        if event.get("type") not in SKIP_PERSIST_TYPES:
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
        log = session.logger

        def _emit_init(event_type: str, data: Dict) -> None:
            if self._is_deleted(session.id):
                return
            self._on_event(session, {
                "type": event_type,
                "session_id": session.id,
                "turn": 0,
                "timestamp": datetime.utcnow().isoformat(),
                "data": data,
            })

        try:
            if self._is_deleted(session.id):
                return

            # --- Agent system ---
            from caribou.agents.AgentSystem import AgentSystem
            blueprint_path = find_blueprint(session.config.agent_system)
            if log:
                log.info("Loading blueprint: %s", blueprint_path)
            agent_sys = AgentSystem.load_from_json(str(blueprint_path))
            if self._is_deleted(session.id):
                return
            session.agent_system = agent_sys

            # Pick driver agent: first agent in the system
            driver_name = next(iter(agent_sys.agents))
            session.driver_agent = agent_sys.get_agent(driver_name)
            session.current_agent = driver_name
            if log:
                log.info("Blueprint loaded | agents: %s | driver: %s", list(agent_sys.agents), driver_name)

            # --- LLM client ---
            if log:
                log.info("Building LLM client | backend: %s", session.config.llm_backend)
            llm_client, model_name = build_llm_client(session.config)
            if self._is_deleted(session.id):
                return
            session.llm_client = llm_client
            session.model_name = model_name
            session.resolved_model = resolve_model_info(
                session.config,
                resolved_model_name=model_name,
            )
            self._save_session(session)
            if log:
                log.info("LLM client ready | backend: %s | model: %s", session.config.llm_backend, model_name)

            # --- Analysis context + initial history ---
            analysis_context = textwrap.dedent(f"""\
                Primary dataset path: **{SANDBOX_DATA_PATH}**
                {"Reference dataset path: **" + SANDBOX_REF_DATA_PATH + "**" if session.config.reference_dataset_path else ""}

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
            if log:
                log.info("Starting sandbox | type: %s", session.config.sandbox_type.value)
            sandbox_started = time.monotonic()
            sandbox_manager = await asyncio.to_thread(
                build_sandbox, session.config, session.output_dir
            )
            if self._is_deleted(session.id):
                try:
                    await asyncio.to_thread(sandbox_manager.stop_container)
                except Exception:
                    pass
                return
            session.sandbox_manager = sandbox_manager
            if log:
                log.info("Sandbox ready | elapsed: %sms", int((time.monotonic() - sandbox_started) * 1000))

            session.status = SessionStatus.idle
            _emit_init("status_change", {"status": "idle", "reason": "ready"})
            if log:
                log.info("Session ready (idle)")

            # Auto sessions with a prompt start immediately — no WebSocket run message needed
            if session.config.mode == SessionMode.auto and session.config.initial_prompt:
                if log:
                    log.info("Auto-mode run starting | prompt: %r", session.config.initial_prompt[:80])
                await self.start_run(session.id, session.config.initial_prompt)

        except BaseException as exc:  # noqa: BLE001 — includes SystemExit/KeyboardInterrupt
            # SystemExit from legacy sandbox helpers must NOT propagate out of
            # this task, or asyncio treats it as a fatal shutdown and takes the
            # server lifespan down with it. Log the failure and forward a clean
            # error event to the UI.
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            if log:
                log.error("Session init failed: %s", exc, exc_info=True)
            session.status = SessionStatus.error
            _emit_init("error", {
                "code": getattr(exc, "code", "INIT_ERROR"),
                "message": str(exc) or exc.__class__.__name__,
                "fatal": True,
                "suggested_fix": getattr(exc, "suggested_fix", None),
            })
            _emit_init("status_change", {"status": "error", "reason": str(exc) or exc.__class__.__name__})


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

session_manager = SessionManager()
