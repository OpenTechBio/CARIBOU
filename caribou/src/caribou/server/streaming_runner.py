"""
Async-compatible event-emitting runner for the CARIBOU web server.

Runs the agent session loop in a background thread and emits structured
events via a thread-safe callback. The server's WebSocket route forwards
those events to the browser.

This is a parallel implementation to execution/runner.py — it shares the
same helpers and execution model but replaces Console output with events.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Token streaming helper
# ---------------------------------------------------------------------------

def _stream_tokens(llm_client, model: str, messages: List[Dict]) -> Any:
    """
    Generator yielding text tokens from the LLM.
    Works with AnthropicClient (has stream_chat) and OpenAI-compatible clients.
    """
    if hasattr(llm_client, "stream_chat"):
        yield from llm_client.stream_chat(
            model=model, messages=messages, temperature=0.0
        )
    else:
        stream = llm_client.chat.completions.create(
            model=model, messages=messages, temperature=0.0, stream=True
        )
        if hasattr(stream, "choices"):
            content = stream.choices[0].message.content
            if content:
                yield content
            return
        for chunk in stream:
            delta = chunk.choices[0].delta
            token = getattr(delta, "content", None)
            if token:
                yield token


def _complete_without_streaming(llm_client, model: str, messages: List[Dict]) -> str:
    response = llm_client.chat.completions.create(
        model=model, messages=messages, temperature=0.0
    )
    return response.choices[0].message.content or ""


def _stream_tokens_with_fallback(llm_client, model: str, messages: List[Dict]) -> Any:
    """
    Stream tokens when possible, but recover from transient chunked-read
    failures by retrying once with a non-streaming request.
    """
    emitted_any = False
    try:
        for token in _stream_tokens(llm_client, model, messages):
            emitted_any = True
            yield token
        return
    except Exception:
        if emitted_any:
            yield "\n\n[Streaming connection interrupted. Retrying request without streaming...]\n\n"

    content = _complete_without_streaming(llm_client, model, messages)
    if content:
        yield content


# ---------------------------------------------------------------------------
# Sync runner (runs inside a ThreadPoolExecutor thread)
# ---------------------------------------------------------------------------

def run_session_sync(
    *,
    session_id: str,
    agent_system,
    driver_agent,
    analysis_context: str,
    llm_client,
    sandbox_manager,
    history: List[Dict],
    is_auto: bool,
    max_turns: int,
    model_name: str,
    output_dir: Path,
    emit: Callable[[Dict], None],
    stop_flag: threading.Event,
    user_input_queue: Optional[queue.Queue] = None,
) -> None:
    """
    Main agent session loop. Replaces Console output with emit() calls.
    Designed to be run in a thread via asyncio.to_thread or ThreadPoolExecutor.
    """
    from caribou.execution.ActionSpace import AgentActionSpace
    from caribou.execution.agent_management import (
        _apply_agent_switch,
        _extract_possible_actions,
    )
    from caribou.execution.message_utils import (
        _code_preview,
        _count_code_blocks,
        _extract_artifacts_from_msg,
        detect_delegation,
        detect_end_session,
        detect_rag,
    )
    from caribou.execution.rag_client import get_rag_client
    from caribou.core.io_helpers import (
        extract_python_code_blocks,
        format_execute_response,
    )
    from rich.console import Console

    console = Console(quiet=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    emitted_artifacts: Set[str] = set()

    def _emit(event_type: str, data: Dict, turn: int = 0) -> None:
        emit({
            "type": event_type,
            "session_id": session_id,
            "turn": turn,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data,
        })

    def _scan_new_artifacts(turn: int) -> None:
        """Emit artifact events for any new files in output_dir."""
        mime_map = {
            ".png": ("plot", "image/png"),
            ".jpg": ("plot", "image/jpeg"),
            ".svg": ("plot", "image/svg+xml"),
            ".pdf": ("plot", "application/pdf"),
            ".csv": ("data", "text/csv"),
            ".h5ad": ("data", "application/x-hdf5"),
            ".txt": ("report", "text/plain"),
        }
        if not output_dir.exists():
            return
        for fpath in sorted(output_dir.iterdir()):
            if not fpath.is_file() or fpath.name in emitted_artifacts:
                continue
            suffix = fpath.suffix.lower()
            art_type, mime_type = mime_map.get(suffix, ("data", "application/octet-stream"))
            emitted_artifacts.add(fpath.name)
            _emit("artifact", {
                "artifact": {
                    "filename": fpath.name,
                    "type": art_type,
                    "mime_type": mime_type,
                    "size_bytes": fpath.stat().st_size,
                    "local_path": str(fpath),
                    "turn": turn,
                }
            }, turn=turn)

    action_space = AgentActionSpace(driver_agent.name)
    action_space.set_possible_actions(_extract_possible_actions(driver_agent))
    action_init_msg = action_space.to_message()
    history.append({"role": "system", "content": action_init_msg})

    current_agent = driver_agent
    turns_completed = 0
    consecutive_failures = 0

    try:
        while True:
            if stop_flag.is_set():
                _emit("status_change", {"status": "stopped", "reason": "user_requested"})
                return

            if is_auto and turns_completed >= max_turns:
                _emit("status_change", {"status": "stopped", "reason": f"max turns reached ({max_turns})"})
                return

            turn = turns_completed + 1
            _emit("status_change", {
                "status": "running",
                "reason": f"turn {turn} — {current_agent.name}",
            }, turn=turn)

            # --- Build cleaned context ---
            cleaned_context = []
            for msg in history:
                m = msg.copy()
                if isinstance(m.get("content"), str):
                    m["content"] = m["content"].rstrip()
                cleaned_context.append(m)

            # --- LLM call (streaming) ---
            try:
                full_msg = ""
                for token in _stream_tokens_with_fallback(llm_client, model_name, cleaned_context):
                    if stop_flag.is_set():
                        break
                    full_msg += token
                    _emit("token", {"agent_name": current_agent.name, "token": token}, turn=turn)
                msg = full_msg
            except Exception as exc:
                _emit("error", {"code": "LLM_ERROR", "message": str(exc), "fatal": True})
                _emit("status_change", {"status": "error", "reason": str(exc)})
                return

            if stop_flag.is_set():
                _emit("status_change", {"status": "stopped", "reason": "user_requested"})
                return

            history.append({"role": "assistant", "content": msg})
            turns_completed += 1

            _emit("message_complete", {
                "message": {
                    "id": f"msg_{session_id}_{turn}",
                    "turn": turn,
                    "role": "assistant",
                    "agent_name": current_agent.name,
                    "content": msg,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            }, turn=turn)

            # --- End session detection ---
            has_delegation = detect_delegation(msg) is not None
            if detect_end_session(msg) and _count_code_blocks(msg) == 0 and not has_delegation:
                if is_auto:
                    _emit("status_change", {"status": "stopped", "reason": "agent_finished"})
                    return
                else:
                    _emit("status_change", {"status": "idle", "reason": "agent_requested_end"})
                    return

            _action_fired = False
            _delegated = False

            # --- RAG ---
            query_str = detect_rag(msg)
            if query_str and current_agent.is_rag_enabled:
                _action_fired = True
                try:
                    rag_client = get_rag_client(console)
                    docs = rag_client.query(query_str)
                    if docs:
                        history.append({"role": "system", "content": docs})
                except Exception:
                    pass

            # --- Delegation ---
            cmd = detect_delegation(msg)
            if cmd and cmd in current_agent.commands:
                _action_fired = True
                target_name = current_agent.commands[cmd].target_agent
                new_agent = agent_system.get_agent(target_name)
                if new_agent:
                    _emit("agent_switch", {
                        "from_agent": current_agent.name,
                        "to_agent": target_name,
                        "command": cmd,
                        "reason": None,
                    }, turn=turn)
                    history.append({
                        "role": "assistant",
                        "content": f"Routing to **{target_name}** (command `{cmd}`)"
                    })
                    _apply_agent_switch(
                        new_agent_prompt=new_agent.get_full_prompt(None),
                        analysis_context=analysis_context,
                        history=history,
                        memory_manager=None,
                        action_space=action_space,
                        new_agent=new_agent,
                    )
                    current_agent = new_agent
                    _delegated = True

            # --- Code execution ---
            code_blocks = extract_python_code_blocks(msg)
            if code_blocks:
                _action_fired = True
                for idx, code in enumerate(code_blocks, start=1):
                    if stop_flag.is_set():
                        break

                    _emit("code_submitted", {
                        "agent_name": current_agent.name,
                        "source": code,
                        "block_index": idx,
                        "total_blocks": len(code_blocks),
                    }, turn=turn)

                    t0 = time.time()
                    exec_result = sandbox_manager.exec_code(code, timeout=600)
                    duration_ms = int((time.time() - t0) * 1000)

                    success = exec_result.get("status") == "ok"
                    consecutive_failures = 0 if success else consecutive_failures + 1

                    _emit("code_result", {
                        "agent_name": current_agent.name,
                        "stdout": exec_result.get("stdout", ""),
                        "stderr": exec_result.get("stderr", ""),
                        "success": success,
                        "duration_ms": duration_ms,
                        "block_index": idx,
                    }, turn=turn)

                    _scan_new_artifacts(turn)

                    feedback = format_execute_response(exec_result, output_dir)
                    action_space.add_action(
                        "code_execution",
                        f"Ran code block {idx}/{len(code_blocks)}:\n{_code_preview(code)}",
                        status=exec_result.get("status"),
                    )
                    history.append({"role": "system", "content": action_space.to_message()})
                    history.append({"role": "assistant", "content": feedback})

            if _delegated and is_auto:
                continue

            if is_auto and not _action_fired:
                no_action_msg = (
                    "[SYSTEM] No action was recognised in your last message. "
                    "Please write executable Python code in a ```python ... ``` block "
                    "or issue a delegation command."
                )
                history.append({"role": "system", "content": no_action_msg})

            if is_auto:
                history.append({"role": "user", "content": "Please continue with the next step."})
                continue

            # --- Interactive: wait for next user message ---
            _emit("status_change", {"status": "idle", "reason": None}, turn=turn)

            if user_input_queue is None:
                return

            # Block until user sends a message or session is stopped
            while True:
                if stop_flag.is_set():
                    _emit("status_change", {"status": "stopped", "reason": "user_requested"})
                    return
                try:
                    user_msg = user_input_queue.get(timeout=1.0)
                    history.append({"role": "user", "content": user_msg})
                    next_turn = turns_completed + 1
                    _emit("message_complete", {
                        "message": {
                            "id": f"msg_{session_id}_user_{next_turn}",
                            "turn": next_turn,
                            "role": "user",
                            "agent_name": "",
                            "content": user_msg,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    }, turn=next_turn)
                    break
                except queue.Empty:
                    continue

    except Exception as exc:
        _emit("error", {"code": "RUNNER_ERROR", "message": str(exc), "fatal": True})
        _emit("status_change", {"status": "error", "reason": str(exc)})


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------

async def run_session_async(
    *,
    session_id: str,
    agent_system,
    driver_agent,
    analysis_context: str,
    llm_client,
    sandbox_manager,
    history: List[Dict],
    is_auto: bool,
    max_turns: int,
    model_name: str,
    output_dir: Path,
    event_callback: Callable[[Dict], None],
    stop_flag: threading.Event,
    user_input_queue: Optional[queue.Queue] = None,
) -> None:
    """
    Runs run_session_sync in a thread so it doesn't block the event loop.
    event_callback is called from the background thread — use
    loop.call_soon_threadsafe() internally if needed.
    """
    loop = asyncio.get_running_loop()

    def _emit(event: Dict) -> None:
        loop.call_soon_threadsafe(event_callback, event)

    await asyncio.to_thread(
        run_session_sync,
        session_id=session_id,
        agent_system=agent_system,
        driver_agent=driver_agent,
        analysis_context=analysis_context,
        llm_client=llm_client,
        sandbox_manager=sandbox_manager,
        history=history,
        is_auto=is_auto,
        max_turns=max_turns,
        model_name=model_name,
        output_dir=output_dir,
        emit=_emit,
        stop_flag=stop_flag,
        user_input_queue=user_input_queue,
    )
