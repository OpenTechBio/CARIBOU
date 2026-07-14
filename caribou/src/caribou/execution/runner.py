# caribou/execution/runner.py
from __future__ import annotations

import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict, cast

from rich.console import Console
from rich.prompt import Prompt

# --- Project-specific Imports ---
try:
    from caribou.agents.AgentSystem import Agent, AgentSystem
    from caribou.core.io_helpers import display, extract_python_code_blocks, format_execute_response
    from caribou.execution.MemoryManager import MemoryManager
    from caribou.execution.ActionSpace import AgentActionSpace
    from caribou.execution.artifacts import SessionArtifacts
    from caribou.execution.agent_management import _extract_possible_actions, _apply_agent_switch
    from caribou.execution.benchmark_runner import run_benchmark
    from caribou.execution.message_utils import (
        detect_delegation,
        detect_end_session,
        detect_rag,
        _extract_artifacts_from_msg,
        _count_code_blocks,
        _code_preview,
    )
    from caribou.execution.path_utils import _init_paths, get_default_runs_dir
    from caribou.execution.report_generation import (
        AgentReportMemory,
        _write_session_report,
        _generate_agent_report,
    )
    from caribou.execution.ui_helpers import _render_todos
except ImportError as e:
    print(f"Failed to import a required CARIBOU module: {e}", file=sys.stderr)
    sys.exit(1)


def get_rag_client(console: Console):
    """Load the optional RAG stack only when a runner action requires it."""
    from caribou.execution.rag_client import get_rag_client as create_rag_client

    return create_rag_client(console)


# Consecutive no-action / code-exec failures we tolerate before ending an auto run.
MAX_CONSECUTIVE_NO_ACTION = 3
MAX_CONSECUTIVE_EXEC_FAILURES = 5
# Retry policy for transient LLM errors.
_LLM_RETRY_ATTEMPTS = 3
_LLM_RETRY_BASE_DELAY = 2.0
_LLM_RETRY_MAX_DELAY = 4.0


class RunnerEvent(TypedDict):
    """Transport-neutral event emitted synchronously by the legacy runner."""

    schema_version: str
    event_type: str
    occurred_at: str
    run_id: str
    turn: int
    agent_name: str
    payload: Dict[str, object]


RunnerEventCallback = Callable[[RunnerEvent], None]
CancellationCheck = Callable[[], bool]


@dataclass(frozen=True)
class AgentSessionResult:
    """Immutable terminal summary returned by :func:`run_agent_session`."""

    schema_version: str
    run_id: str
    succeeded: bool
    cancelled: bool
    end_reason: str
    turns_completed: int
    code_blocks_produced: int
    code_exec_attempts: int
    code_exec_failures: int
    correction_count: int
    current_agent_name: str
    final_turn: int
    started_at: str
    ended_at: str
    duration_seconds: float


class _LlmCallCancelled(Exception):
    """Internal control-flow signal for cooperative cancellation during an LLM call."""


_UNSUCCESSFUL_END_REASONS = frozenset(
    {
        "cancelled",
        "llm_error",
        "max_turns_reached",
        "stuck_no_action",
        "stuck_code_failures",
        "timeout",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit_runner_event(
    callback: Optional[RunnerEventCallback],
    *,
    event_type: str,
    run_id: str,
    turn: int,
    agent_name: str,
    payload: Optional[Dict[str, object]] = None,
) -> None:
    if callback is None:
        return
    callback(
        {
            "schema_version": "caribou.runner_event.v1",
            "event_type": event_type,
            "occurred_at": _utc_now(),
            "run_id": run_id,
            "turn": turn,
            "agent_name": agent_name,
            "payload": dict(payload or {}),
        }
    )


def _cancellation_requested(should_cancel: Optional[CancellationCheck]) -> bool:
    return should_cancel is not None and should_cancel()


def _retry_backoff_cancelled(
    delay: float, should_cancel: Optional[CancellationCheck]
) -> bool:
    """Wait for a retry without making cooperative cancellation unresponsive."""
    if should_cancel is None:
        time.sleep(delay)
        return False
    deadline = time.monotonic() + delay
    while True:
        if should_cancel():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def _call_llm_with_retry(
    *,
    console: Console,
    llm_client: object,
    model_name: str,
    messages: List[Dict[str, str]],
    should_cancel: Optional[CancellationCheck] = None,
    retry_attempts: int = _LLM_RETRY_ATTEMPTS,
    retry_base_delay: float = _LLM_RETRY_BASE_DELAY,
    retry_max_delay: float = _LLM_RETRY_MAX_DELAY,
    request_timeout_seconds: float | None = None,
) -> Optional[str]:
    """
    Call the LLM with exponential backoff on transient errors. Returns the
    assistant message string, or None if all retries are exhausted.

    When the optional cancellation hook is used, cancellation raises the private
    ``_LlmCallCancelled`` control-flow signal for ``run_agent_session`` to handle.
    """
    if retry_attempts < 1 or retry_base_delay < 0 or retry_max_delay < retry_base_delay:
        raise ValueError("invalid LLM retry policy")
    if request_timeout_seconds is not None and request_timeout_seconds <= 0:
        raise ValueError("request timeout must be greater than zero")
    last_exc: Optional[Exception] = None
    for attempt in range(1, retry_attempts + 1):
        if _cancellation_requested(should_cancel):
            raise _LlmCallCancelled
        try:
            request_options: Dict[str, object] = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.0,
            }
            if request_timeout_seconds is not None:
                request_options["timeout"] = request_timeout_seconds
            resp = cast(Any, llm_client).chat.completions.create(**request_options)
            if _cancellation_requested(should_cancel):
                raise _LlmCallCancelled
            return resp.choices[0].message.content
        except _LlmCallCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — SDKs raise varied exception types
            last_exc = e
            if attempt >= retry_attempts:
                break
            delay = min(retry_base_delay * (2 ** (attempt - 1)), retry_max_delay)
            console.print(
                f"[yellow]LLM API error (attempt {attempt}/{retry_attempts}): {e}. "
                f"Retrying in {delay:.1f}s…[/yellow]"
            )
            if _retry_backoff_cancelled(delay, should_cancel):
                raise _LlmCallCancelled
    console.print(f"[red]LLM API error after {retry_attempts} attempts: {last_exc}[/red]")
    return None


# --- Type Hinting & Base Classes ---
class SandboxManager:
    """Abstract base class for sandbox interaction."""
    def start_container(self) -> bool:
        raise NotImplementedError

    def stop_container(self) -> None:
        raise NotImplementedError

    def exec_code(self, code: str, timeout: int) -> dict:
        raise NotImplementedError


# --- Core Runner Functions ---
def run_agent_session(
    *,
    console: Console,
    agent_system: AgentSystem,
    driver_agent: Agent,
    analysis_context: str,
    llm_client: object,
    sandbox_manager: SandboxManager,
    history: List[Dict[str, str]],
    is_auto: bool,
    compress_memory: bool = False,
    max_turns: int = 1,
    model_name: str = "gpt-5.2",
    benchmark_modules: Optional[List[str]] = None,
    output_dir: Optional[Path] = None,
    make_report: bool = False,
    agent_report_memory: bool = False,
    durable_run_id: Optional[str] = None,
    should_cancel: Optional[CancellationCheck] = None,
    event_callback: Optional[RunnerEventCallback] = None,
    timeout_seconds: Optional[float] = None,
    max_consecutive_no_action: int = MAX_CONSECUTIVE_NO_ACTION,
    max_consecutive_exec_failures: int = MAX_CONSECUTIVE_EXEC_FAILURES,
    llm_retry_attempts: int = _LLM_RETRY_ATTEMPTS,
    llm_retry_base_delay: float = _LLM_RETRY_BASE_DELAY,
    llm_retry_max_delay: float = _LLM_RETRY_MAX_DELAY,
) -> AgentSessionResult:
    """
    Main driver for agent execution sessions, passing output_dir for benchmark saving.
    """
    _init_paths(output_dir)

    if durable_run_id is not None and not durable_run_id.strip():
        raise ValueError("durable_run_id must be non-empty when provided")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if max_consecutive_no_action < 1 or max_consecutive_exec_failures < 1:
        raise ValueError("consecutive failure limits must be positive")
    run_id = durable_run_id or f"run_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    default_runs_dir = get_default_runs_dir()
    artifacts_dir = output_dir if output_dir else (default_runs_dir / "session_notes" / run_id)
    artifacts = SessionArtifacts(run_id=run_id, base_dir=artifacts_dir)

    if agent_report_memory and compress_memory:
        console.print("[yellow]Agent report memory enabled; disabling episodic compression for this session.[/yellow]")
        compress_memory = False

    memory_manager: Optional[MemoryManager] = None
    if compress_memory:
        console.print("[bold cyan]🧠 Adaptive context memory is enabled.[/bold cyan]")
        memory_manager = MemoryManager(llm_client=llm_client, model_name=model_name, initial_history=history)

    report_memory: Optional[AgentReportMemory] = None
    current_agent_history_start = 0
    if agent_report_memory:
        base_globals = [history[0]] if history else []
        agent_prompt_content = history[1]["content"] if len(history) > 1 else ""
        report_memory = AgentReportMemory(base_globals, agent_prompt_content)
        current_agent_history_start = min(len(history), 2)

    action_space = AgentActionSpace(driver_agent.name)
    action_space.set_possible_actions(_extract_possible_actions(driver_agent))
    action_init_msg = action_space.to_message()
    history.append({"role": "system", "content": action_init_msg})
    if memory_manager:
        memory_manager.add_message("system", action_init_msg)
    if agent_report_memory:
        current_agent_history_start = min(len(history), 2)

    # --- Display the initial context provided by the CLI ---
    for message in history:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        if role in ["system", "user"]:
            display(console, role, content)

    current_agent = driver_agent
    turns_completed = 0
    final_turn = 0
    code_block_count = 0
    code_exec_attempts = 0
    code_exec_failures = 0
    consecutive_failures = 0
    consecutive_no_action = 0
    correction_count = 0
    session_start_ts = _utc_now()
    session_start_time = time.monotonic()
    session_deadline = (
        session_start_time + timeout_seconds
        if timeout_seconds is not None
        else None
    )

    def session_stop_reason() -> str | None:
        if _cancellation_requested(should_cancel):
            return "cancelled"
        if session_deadline is not None and time.monotonic() >= session_deadline:
            return "timeout"
        return None

    session_end_reason = "completed"
    last_code_snippet: str | None = None

    while True:
        stop_reason = session_stop_reason()
        if stop_reason is not None:
            session_end_reason = stop_reason
            break
        if is_auto and turns_completed >= max_turns:
            console.print("[bold green]Auto run finished: Max turns reached.[/bold green]")
            session_end_reason = "max_turns_reached"
            break
        turn = turns_completed + 1
        final_turn = turn
        console.print(f"\n[bold]LLM call (turn {turn})…[/bold]")
        _emit_runner_event(
            event_callback,
            event_type="turn_started",
            run_id=run_id,
            turn=turn,
            agent_name=current_agent.name,
            payload={"model_name": model_name},
        )

        if report_memory:
            working_history = history[current_agent_history_start:]
            context_to_send = report_memory.build_context(working_history)
        elif memory_manager:
            context_to_send = memory_manager.get_context()
        else:
            context_to_send = history
        # Claude requires that assistant messages don't end with trailing whitespace
        cleaned_context = []
        for context_message in context_to_send:
            cleaned_msg = context_message.copy()
            if "content" in cleaned_msg and isinstance(cleaned_msg["content"], str):
                cleaned_msg["content"] = cleaned_msg["content"].rstrip()
            cleaned_context.append(cleaned_msg)

        try:
            request_timeout_seconds = (
                max(0.001, session_deadline - time.monotonic())
                if session_deadline is not None
                else None
            )
            msg = _call_llm_with_retry(
                console=console,
                llm_client=llm_client,
                model_name=model_name,
                messages=cleaned_context,
                should_cancel=lambda: session_stop_reason() is not None,
                retry_attempts=llm_retry_attempts,
                retry_base_delay=llm_retry_base_delay,
                retry_max_delay=llm_retry_max_delay,
                request_timeout_seconds=request_timeout_seconds,
            )
        except _LlmCallCancelled:
            session_end_reason = session_stop_reason() or "cancelled"
            break
        if msg is None:
            session_end_reason = "llm_error"
            break

        history.append({"role": "assistant", "content": msg})
        if memory_manager:
            memory_manager.add_message("assistant", msg)
        display(console, f"assistant ({current_agent.name})", msg)
        turns_completed += 1
        _emit_runner_event(
            event_callback,
            event_type="assistant_message",
            run_id=run_id,
            turn=turn,
            agent_name=current_agent.name,
            payload={"role": "assistant", "content": msg},
        )

        stop_reason = session_stop_reason()
        if stop_reason is not None:
            session_end_reason = stop_reason
            break

        blocks_found = _count_code_blocks(msg)
        if blocks_found:
            code_block_count += blocks_found

        # --- Artifact extraction (notes, TODOs) ---
        extracted_notes, extracted_todos = _extract_artifacts_from_msg(msg)
        if extracted_notes:
            for note in extracted_notes:
                artifacts.add_note(note, current_agent.name, turn)
                note_msg = f"Captured note (turn {turn}, agent {current_agent.name}): {note}"
                history.append({"role": "system", "content": note_msg})
                if memory_manager:
                    memory_manager.add_message("system", note_msg)
            action_space.add_action("note_logged", f"Logged {len(extracted_notes)} note(s).", status="ok")
        if extracted_todos:
            for todo_text in extracted_todos:
                item = artifacts.add_todo(todo_text, current_agent.name, turn)
                todo_msg = f"TODO added (#{item.id}) by {current_agent.name}: {item.text}"
                history.append({"role": "system", "content": todo_msg})
                if memory_manager:
                    memory_manager.add_message("system", todo_msg)
            action_space.add_action("todo_logged", f"Logged {len(extracted_todos)} TODO(s).", status="ok")

        # --- End session handling ---
        # Only end session if there's no delegation command also present
        # (prevents premature exit when LLM outputs both delegation and end_session)
        has_delegation = detect_delegation(msg) is not None
        if detect_end_session(msg) and _count_code_blocks(msg) == 0 and not has_delegation:
            if is_auto:
                console.print("[yellow]Agent requested end_session. Ending auto run.[/yellow]")
                session_end_reason = "agent_finished"
                break
            else:
                user_choice = Prompt.ask(
                    "Agent requested end_session. Continue anyway?",
                    choices=["y", "n"],
                    default="n",
                ).lower()
                if user_choice == "n":
                    console.print("[bold yellow]Ending session at agent request.[/bold yellow]")
                    session_end_reason = "agent_finished"
                    break

        # Track whether any substantive action fires this turn (for loop-detection feedback)
        _action_fired = False
        _delegated = False

        # --- RAG handling ---
        query_from_re = detect_rag(msg)
        if query_from_re and current_agent.is_rag_enabled:
            _action_fired = True
            console.print(f"[yellow]🔍 Triggering RAG query: {query_from_re}[/yellow]")
            _emit_runner_event(
                event_callback,
                event_type="rag_attempt",
                run_id=run_id,
                turn=turn,
                agent_name=current_agent.name,
                payload={"query": query_from_re, "kind": "knowledge_query"},
            )
            rag_error: Optional[Exception] = None
            try:
                rag_client = get_rag_client(console)
                retrieved_docs = rag_client.query(query_from_re)
            except Exception as rag_exc:  # noqa: BLE001 — surface, don't swallow
                rag_error = rag_exc
                console.print(f"[red] RAG query failed: {rag_exc} [/red]")
                rag_err = (
                    f"[SYSTEM] RAG query for '{query_from_re}' failed: {rag_exc}. "
                    f"Proceed without retrieved context."
                )
                history.append({"role": "system", "content": rag_err})
                if memory_manager:
                    memory_manager.add_message("system", rag_err)
                retrieved_docs = None
            _emit_runner_event(
                event_callback,
                event_type="rag_result",
                run_id=run_id,
                turn=turn,
                agent_name=current_agent.name,
                payload={
                    "query": query_from_re,
                    "kind": "knowledge_query",
                    "success": bool(retrieved_docs),
                    "content": retrieved_docs or "",
                    "error": str(rag_error) if rag_error else "",
                },
            )
            if retrieved_docs:
                console.print("[green] RAG query successful. [/green]")
                feedback = retrieved_docs
                console.print(feedback)
                if memory_manager:
                    memory_manager.add_message("system", feedback)
                history.append({"role": "system", "content": feedback})
            else:
                console.print("[red] RAG query unsuccessful. [/red]")

            stop_reason = session_stop_reason()
            if stop_reason is not None:
                session_end_reason = stop_reason
                break

        cmd = detect_delegation(msg)
        if cmd and cmd in current_agent.commands:
            _action_fired = True
            target_agent_name = current_agent.commands[cmd].target_agent
            new_agent = agent_system.get_agent(target_agent_name)
            if new_agent:
                previous_agent_name = current_agent.name
                if report_memory:
                    agent_history_slice = history[current_agent_history_start:]
                    agent_report = _generate_agent_report(
                        console,
                        llm_client=llm_client,
                        model_name=model_name,
                        agent_name=current_agent.name,
                        history_slice=agent_history_slice,
                    )
                    if agent_report:
                        report_memory.add_report(current_agent.name, agent_report)
                        history.append({"role": "system", "content": f"Agent report from {current_agent.name}:\n{agent_report}"})
                    current_agent_history_start = len(history)
                routing_message = f"🔄 Routing to '{target_agent_name}' via {cmd}"
                current_agent = new_agent
                # Global policy lives in the pinned first system message; skip re-embedding here.
                system_prompt = current_agent.get_full_prompt(None)
                prompt_with_context = system_prompt + "\n\n" + analysis_context
                console.print(f"[yellow]{routing_message}[/yellow]")
                history.append({"role": "assistant", "content": f"🔄 Routing to **{target_agent_name}** (command `{cmd}`)"})
                if memory_manager:
                    memory_manager.add_message("assistant", routing_message)
                _apply_agent_switch(
                    new_agent_prompt=system_prompt,
                    analysis_context=analysis_context,
                    history=history,
                    memory_manager=memory_manager,
                    action_space=action_space,
                    new_agent=new_agent,
                )
                if report_memory:
                    report_memory.update_agent_prompt(prompt_with_context)
                    current_agent_history_start = len(history)
                _emit_runner_event(
                    event_callback,
                    event_type="agent_switch",
                    run_id=run_id,
                    turn=turn,
                    agent_name=current_agent.name,
                    payload={
                        "from_agent": previous_agent_name,
                        "to_agent": target_agent_name,
                        "command": cmd,
                    },
                )
                _delegated = True

        stop_reason = session_stop_reason()
        if stop_reason is not None:
            session_end_reason = stop_reason
            break

        code_blocks = extract_python_code_blocks(msg)
        if code_blocks:
            _action_fired = True
            total_blocks = len(code_blocks)
            rag_short_circuit = False
            cancelled_during_actions = False
            for idx, code in enumerate(code_blocks, start=1):
                if session_stop_reason() is not None:
                    cancelled_during_actions = True
                    break
                last_code_snippet = code
                console.print("[cyan]Executing code in sandbox…[/cyan]")
                action_id = f"{run_id}:turn:{turn}:block:{idx}"
                _emit_runner_event(
                    event_callback,
                    event_type="code_submitted",
                    run_id=run_id,
                    turn=turn,
                    agent_name=current_agent.name,
                    payload={
                        "action_id": action_id,
                        "source": code,
                        "block_index": idx,
                        "total_blocks": total_blocks,
                    },
                )
                code_started = time.monotonic()
                code_timeout = 600
                if session_deadline is not None:
                    code_timeout = max(
                        1,
                        min(600, math.ceil(session_deadline - time.monotonic())),
                    )
                exec_result = sandbox_manager.exec_code(code, timeout=code_timeout)
                code_duration_ms = max(0, int((time.monotonic() - code_started) * 1000))
                code_exec_attempts += 1
                if exec_result.get("status") != "ok":
                    code_exec_failures += 1
                    consecutive_failures += 1
                    if consecutive_failures == 2:
                        correction_count += 1
                else:
                    consecutive_failures = 0
                feedback = format_execute_response(exec_result, output_dir if output_dir else get_default_runs_dir())
                if total_blocks > 1:
                    feedback = feedback.replace(
                        "Code execution result:",
                        f"Code execution result (block {idx}/{total_blocks}):",
                        1,
                    )
                if memory_manager:
                    memory_manager.add_message("system", feedback)
                    if exec_result.get("status") == "ok":
                        memory_manager.add_pivotal_code(code)
                action_label = "Ran code block"
                if total_blocks > 1:
                    action_label = f"Ran code block {idx}/{total_blocks}"
                action_space.add_action(
                    "code_execution",
                    f"{action_label}:\n{_code_preview(code)}",
                    status=exec_result.get("status"),
                )
                summary_msg = action_space.to_message()
                history.append({"role": "system", "content": summary_msg})
                if memory_manager:
                    memory_manager.add_message("system", summary_msg)
                history.append({"role": "assistant", "content": feedback})
                display(console, "code execution result", feedback)
                _emit_runner_event(
                    event_callback,
                    event_type="code_result",
                    run_id=run_id,
                    turn=turn,
                    agent_name=current_agent.name,
                    payload={
                        "action_id": action_id,
                        "success": exec_result.get("status") == "ok",
                        "status": str(exec_result.get("status", "unknown")),
                        "duration_ms": code_duration_ms,
                        "stdout": str(exec_result.get("stdout", "")),
                        "stderr": str(exec_result.get("stderr", "")),
                        "block_index": idx,
                        "total_blocks": total_blocks,
                    },
                )

                stderr = exec_result.get("stderr", "")
                if stderr and current_agent.is_rag_enabled:
                    func_error_patterns = [
                        r"(\w+)\(.*\) missing \d+ required positional argument",  # TypeError missing arguments
                        r"NameError: name '(\w+)' is not defined",  # NameError
                        r"AttributeError: .* has no attribute '(\w+)'",  # AttributeError
                        r"'(\w+)\(.*\) got an unexpected keyword argument",  # Unexpected keyword argument
                    ]

                    function_name = ""
                    retrieved_docs = ""

                    for pat in func_error_patterns:
                        match = re.search(pat, stderr)
                        if match:
                            function_name = next(
                                (group for group in match.groups() if group), ""
                            )
                            break

                    if function_name:
                        console.print(
                            f"[yellow]🔍 Incorrect function signature detected: {function_name}, function database search...[/yellow]"
                        )
                        rag_client = get_rag_client(console)
                        retrieved_docs = rag_client.retrieve_function(function_name)
                        if retrieved_docs:
                            console.print("[green] Query successful - Function signature found. [/green]")
                            feedback += (
                                f"\n {function_name} produced an error. The correct function signature for "
                                f"{function_name} is:\n{retrieved_docs}"
                            )
                            history.append({"role": "system", "content": feedback})
                            rag_short_circuit = True
                            break
                        else:
                            print("Error Query unsuccessful - Function signature does not exist in the current database.")
                if session_stop_reason() is not None:
                    cancelled_during_actions = True
                    break
            if cancelled_during_actions:
                session_end_reason = session_stop_reason() or "cancelled"
                break
            if rag_short_circuit:
                continue
            # Escalate if the same code path keeps failing — don't loop forever.
            if is_auto and consecutive_failures >= max_consecutive_exec_failures:
                console.print(
                    f"[bold red]Auto run halted: {consecutive_failures} consecutive "
                    f"code execution failures — likely stuck on the same error.[/bold red]"
                )
                escalation_msg = (
                    f"[SYSTEM] {consecutive_failures} consecutive code execution failures. "
                    f"The current approach is not working — ending this auto run so a human "
                    f"can inspect the state."
                )
                history.append({"role": "system", "content": escalation_msg})
                if memory_manager:
                    memory_manager.add_message("system", escalation_msg)
                session_end_reason = "stuck_code_failures"
                break

        # In auto mode, delegation immediately hands execution to the new agent.
        # In interactive mode, the user gets control back after the handoff.
        if _delegated and is_auto:
            continue

        if is_auto and not _action_fired:
            # No action was taken this turn — give the LLM explicit feedback so it
            # doesn't silently repeat the same output indefinitely.
            consecutive_no_action += 1
            if consecutive_no_action >= max_consecutive_no_action:
                # The agent is stuck emitting non-actionable text. End the run
                # rather than burn tokens looping on the same feedback message.
                console.print(
                    f"[bold red]Auto run halted: agent produced no action for "
                    f"{consecutive_no_action} consecutive turns.[/bold red]"
                )
                session_end_reason = "stuck_no_action"
                break
            rag_hint = ""
            if current_agent.is_rag_enabled:
                rag_hint = (
                    " To query the knowledge base write `query_rag_<topic>` "
                    "(with angle brackets) on its own line, e.g. `query_rag_<Celltyping API>`."
                )
            no_action_msg = (
                f"[SYSTEM] No action was recognised in your last message "
                f"(no Python code block, no delegation command, no RAG query).{rag_hint} "
                f"Please either write executable Python code in a ```python ... ``` block, "
                f"issue a delegation command, or use a RAG query. "
                f"Do not output plain text descriptions of what you intend to do. "
                f"(Attempt {consecutive_no_action}/{max_consecutive_no_action} — the run "
                f"will halt after {max_consecutive_no_action} consecutive no-action turns.)"
            )
            console.print(f"[red]{no_action_msg}[/red]")
            history.append({"role": "system", "content": no_action_msg})
            if memory_manager:
                memory_manager.add_message("system", no_action_msg)
        elif _action_fired:
            consecutive_no_action = 0

        if is_auto:
            if benchmark_modules:
                stop_reason = session_stop_reason()
                if stop_reason is not None:
                    session_end_reason = stop_reason
                    break
                # Determine output_dir for benchmark
                bench_output_dir = output_dir if output_dir else get_default_runs_dir()
                result_str = run_benchmark(
                    console, sandbox_manager, benchmark_modules[0],
                    is_auto=True, metadata={"name": "auto"}, agent_name=current_agent.name,
                    code_snippet=last_code_snippet, output_dir=bench_output_dir
                )
                if memory_manager:
                    memory_manager.add_message("system", result_str)
                history.append({"role": "system", "content": result_str})
                display(console, "user", result_str)
                stop_reason = session_stop_reason()
                if stop_reason is not None:
                    session_end_reason = stop_reason
                    break

            # Add a user message to continue the conversation in auto mode
            auto_continue_msg = "Please continue with the next step."
            history.append({"role": "user", "content": auto_continue_msg})
            if memory_manager:
                memory_manager.add_message("user", auto_continue_msg)

            console.print(f"[yellow]Auto-continuing... {turns_completed}/{max_turns} turns complete.[/yellow]")
            continue

        # Interactive mode: prompt user for next action
        while True:
            stop_reason = session_stop_reason()
            if stop_reason is not None:
                session_end_reason = stop_reason
                break
            prompt_text = "\n[bold]Next message ('benchmark' to run selected benchmark, 'exit' to quit)[/bold]"
            try:
                user_input = Prompt.ask(prompt_text, default="").strip()
            except (EOFError, KeyboardInterrupt):
                user_input = "exit"

            if user_input.lower() in {"exit", "quit"}:
                console.print("[bold yellow]Exiting session.[/bold yellow]")
                session_end_reason = "user_exit"
                break

            # --- Quick commands for TODO management ---
            if user_input.lower().startswith("/todo"):
                todo_text = user_input[len("/todo"):].strip()
                if todo_text:
                    todo_item = artifacts.add_todo(todo_text, "user", turn)
                    todo_message = (
                        f"TODO added (#{todo_item.id}) by user: {todo_item.text}"
                    )
                    history.append({"role": "system", "content": todo_message})
                    if memory_manager:
                        memory_manager.add_message("system", todo_message)
                    console.print(
                        f"[green]Added TODO #[/green]{todo_item.id}: {todo_item.text}"
                    )
                else:
                    console.print("[yellow]Usage: /todo <task>[/yellow]")
                continue

            if user_input.lower().startswith("/done"):
                parts = user_input.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    todo_id = int(parts[1])
                    completed_item = artifacts.complete_todo(todo_id)
                    if completed_item:
                        todo_message = (
                            f"TODO completed (#{completed_item.id}) by user"
                        )
                        history.append({"role": "system", "content": todo_message})
                        if memory_manager:
                            memory_manager.add_message("system", todo_message)
                        console.print(f"[green]Marked TODO #[/green]{todo_id} as done")
                    else:
                        console.print(f"[yellow]No TODO found with id {todo_id}[/yellow]")
                else:
                    console.print("[yellow]Usage: /done <id>[/yellow]")
                continue

            if user_input.lower() in {"/todos", "todos"}:
                todo_items = [
                    {
                        "id": t.id,
                        "text": t.text,
                        "status": t.status,
                        "added_by": t.added_by,
                        "turn": t.turn,
                    }
                    for t in artifacts.list_todos()
                ]
                _render_todos(console, todo_items)
                continue

            if user_input.lower() == "benchmark":
                if benchmark_modules:
                    bench_output_dir = output_dir if output_dir else get_default_runs_dir()
                    for bm_module in benchmark_modules:
                        run_benchmark(console, sandbox_manager, bm_module, is_auto=False, output_dir=bench_output_dir)
                    continue
                else:
                    console.print("[yellow]No benchmark modules were specified at startup.[/yellow]")
                    continue

            if user_input:
                if memory_manager:
                    memory_manager.add_message("user", user_input)
                history.append({"role": "user", "content": user_input})
                display(console, "user", user_input)
            break

        # if we broke out of the inner prompt loop due to exit, stop the session
        if session_end_reason == "user_exit":
            break

    session_end_ts = _utc_now()
    duration_seconds = round(time.monotonic() - session_start_time, 2)

    if make_report:
        session_stats: dict[str, object] = {
            "mode": "auto" if is_auto else "interactive",
            "driver_agent": driver_agent.name,
            "model": model_name,
            "agent_turns": turns_completed,
            "code_blocks_produced": code_block_count,
            "code_exec_attempts": code_exec_attempts,
            "code_exec_failures": code_exec_failures,
            "correction_count": correction_count,
            "session_start": session_start_ts,
            "session_end": session_end_ts,
            "duration_seconds": duration_seconds,
            "max_turns_requested": max_turns if is_auto else None,
            "end_reason": session_end_reason,
        }
        report_output_dir = output_dir if output_dir else get_default_runs_dir()
        _write_session_report(console, output_dir=report_output_dir, stats=session_stats)

    result = AgentSessionResult(
        schema_version="caribou.agent_session_result.v1",
        run_id=run_id,
        succeeded=session_end_reason not in _UNSUCCESSFUL_END_REASONS,
        cancelled=session_end_reason == "cancelled",
        end_reason=session_end_reason,
        turns_completed=turns_completed,
        code_blocks_produced=code_block_count,
        code_exec_attempts=code_exec_attempts,
        code_exec_failures=code_exec_failures,
        correction_count=correction_count,
        current_agent_name=current_agent.name,
        final_turn=final_turn,
        started_at=session_start_ts,
        ended_at=session_end_ts,
        duration_seconds=duration_seconds,
    )
    _emit_runner_event(
        event_callback,
        event_type="session_end",
        run_id=run_id,
        turn=final_turn,
        agent_name=current_agent.name,
        payload={
            "succeeded": result.succeeded,
            "cancelled": result.cancelled,
            "end_reason": result.end_reason,
            "turns_completed": result.turns_completed,
            "code_blocks_produced": result.code_blocks_produced,
            "code_exec_attempts": result.code_exec_attempts,
            "code_exec_failures": result.code_exec_failures,
            "correction_count": result.correction_count,
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "duration_seconds": result.duration_seconds,
        },
    )
    return result
