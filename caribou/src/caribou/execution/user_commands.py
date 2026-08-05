"""
User-typed REPL commands for the interactive `caribou run` session.

These are commands the *human* types at the interactive prompt (`/todo`,
`/evaluate`, ...). They are unrelated to agent delegation commands
(`delegate_to_<agent>`), which are emitted by the LLM itself and dispatched in
runner.py by matching `detect_delegation()` against `Agent.commands` from the
blueprint JSON — that mechanism has no human input in the loop at all.

Dispatch is exact-match on the first whitespace-delimited token (after
lowercasing), not prefix matching: `/todo` and `/todos` are different tokens,
so a former startswith("/todo") check that also matched "/todos" can't
recur here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel

from caribou.agents.AgentSystem import Agent, AgentSystem
from caribou.execution.artifacts import SessionArtifacts
from caribou.execution.benchmark_runner import run_benchmark
from caribou.execution.evaluation import (
    EvaluationContextTooLarge,
    build_evaluation_payload,
    resolve_evaluator_agent,
    run_evaluation,
)
from caribou.execution.MemoryManager import MemoryManager
from caribou.execution.path_utils import get_default_runs_dir
from caribou.execution.report_generation import AgentReportMemory
from caribou.execution.token_utils import estimate_messages_tokens
from caribou.execution.ui_helpers import _render_todos


@dataclass
class UserCommandContext:
    """Everything a REPL command handler needs, gathered fresh each time the
    interactive prompt loop asks for the next user turn (current_agent can
    change between turns via delegation, so this must not be built once and
    reused)."""

    console: Console
    run_id: str
    turn: int
    history: List[Dict[str, str]]
    artifacts: SessionArtifacts
    agent_system: AgentSystem
    current_agent: Agent
    llm_client: object
    model_name: str
    memory_manager: Optional[MemoryManager]
    report_memory: Optional[AgentReportMemory]
    benchmark_modules: Optional[List[str]]
    sandbox_manager: object
    output_dir: Optional[Path]


@dataclass
class UserCommand:
    name: str  # canonical form, e.g. "/evaluate"
    aliases: Tuple[str, ...]
    help: str
    handler: Callable[[str, "UserCommandContext"], None]


def _cmd_evaluate(_arg: str, ctx: UserCommandContext) -> None:
    console = ctx.console
    evaluator_agent, source = resolve_evaluator_agent(ctx.agent_system)
    console.print(f"[dim]Using {source}.[/dim]")

    payload = build_evaluation_payload(
        run_id=ctx.run_id,
        turn=ctx.turn,
        active_agent=ctx.current_agent.name,
        history=ctx.history,
        todos=[
            {
                "id": t.id,
                "text": t.text,
                "status": t.status,
                "added_by": t.added_by,
                "turn": t.turn,
            }
            for t in ctx.artifacts.list_todos()
        ],
    )

    console.print("[cyan]Evaluating run...[/cyan]")
    try:
        assessment = run_evaluation(
            evaluator_agent=evaluator_agent,
            llm_client=ctx.llm_client,
            model_name=ctx.model_name,
            payload=payload,
        )
    except EvaluationContextTooLarge as exc:
        console.print(
            f"[red]{exc} Evaluation aborted; nothing was sent.[/red]"
        )
        return

    console.print(Panel(assessment, title="Run Evaluation", border_style="cyan"))

    report_output_dir = ctx.output_dir if ctx.output_dir else get_default_runs_dir()
    report_dir = report_output_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = (
        report_dir / f"evaluation_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    )
    report_path.write_text(
        json.dumps(
            {
                "run_id": ctx.run_id,
                "turn": ctx.turn,
                "evaluator_agent": evaluator_agent.name,
                "model": ctx.model_name,
                "assessment": assessment,
            },
            indent=2,
        )
    )
    console.print(f"[bold green]✓ Evaluation saved to:[/bold green] {report_path}")


def _format_state_value(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _print_memory_state(console: Console, state: Dict[str, object], label: str) -> None:
    breakdown = state.get("context_breakdown", {})
    top_lines = [
        f"{k}: {_format_state_value(v)}"
        for k, v in state.items()
        if k != "context_breakdown"
    ]
    breakdown_lines = [f"  {k}: {_format_state_value(v)}" for k, v in breakdown.items()]
    body = "\n".join(top_lines) + "\n\n-- context breakdown --\n" + "\n".join(breakdown_lines)
    console.print(Panel(body, title=f"Memory State ({label})", border_style="magenta"))


def _cmd_memory(_arg: str, ctx: UserCommandContext) -> None:
    console = ctx.console
    if ctx.memory_manager is not None:
        _print_memory_state(console, ctx.memory_manager.get_state(), "episodic")
    elif ctx.report_memory is not None:
        _print_memory_state(console, ctx.report_memory.get_state(), "agent_report")
    else:
        tokens = estimate_messages_tokens(ctx.history)
        console.print(
            Panel(
                f"strategy: full\nmessages: {len(ctx.history)}\nestimated_tokens: {tokens}",
                title="Memory State (full)",
                border_style="magenta",
            )
        )


def _cmd_todo(arg: str, ctx: UserCommandContext) -> None:
    todo_text = arg.strip()
    if not todo_text:
        ctx.console.print("[yellow]Usage: /todo <task>[/yellow]")
        return
    todo_item = ctx.artifacts.add_todo(todo_text, "user", ctx.turn)
    todo_message = f"TODO added (#{todo_item.id}) by user: {todo_item.text}"
    ctx.history.append({"role": "system", "content": todo_message})
    if ctx.memory_manager:
        ctx.memory_manager.add_message("system", todo_message)
    ctx.console.print(f"[green]Added TODO #[/green]{todo_item.id}: {todo_item.text}")


def _cmd_done(arg: str, ctx: UserCommandContext) -> None:
    parts = arg.split()
    if not parts or not parts[0].isdigit():
        ctx.console.print("[yellow]Usage: /done <id>[/yellow]")
        return
    todo_id = int(parts[0])
    completed_item = ctx.artifacts.complete_todo(todo_id)
    if not completed_item:
        ctx.console.print(f"[yellow]No TODO found with id {todo_id}[/yellow]")
        return
    todo_message = f"TODO completed (#{completed_item.id}) by user"
    ctx.history.append({"role": "system", "content": todo_message})
    if ctx.memory_manager:
        ctx.memory_manager.add_message("system", todo_message)
    ctx.console.print(f"[green]Marked TODO #[/green]{todo_id} as done")


def _cmd_todos(_arg: str, ctx: UserCommandContext) -> None:
    todo_items = [
        {
            "id": t.id,
            "text": t.text,
            "status": t.status,
            "added_by": t.added_by,
            "turn": t.turn,
        }
        for t in ctx.artifacts.list_todos()
    ]
    _render_todos(ctx.console, todo_items)


def _cmd_artifacts(_arg: str, ctx: UserCommandContext) -> None:
    base_dir = ctx.artifacts.base_dir
    files = sorted(p for p in base_dir.rglob("*") if p.is_file())
    if not files:
        ctx.console.print(f"[yellow]No artifact files found under {base_dir}[/yellow]")
        return
    lines = [
        f"{p.relative_to(base_dir)}  ({p.stat().st_size:,} bytes)" for p in files
    ]
    ctx.console.print(
        Panel("\n".join(lines), title=f"Artifacts ({base_dir})", border_style="green")
    )


def _cmd_benchmark(_arg: str, ctx: UserCommandContext) -> None:
    if not ctx.benchmark_modules:
        ctx.console.print(
            "[yellow]No benchmark modules were specified at startup.[/yellow]"
        )
        return
    bench_output_dir = ctx.output_dir if ctx.output_dir else get_default_runs_dir()
    for bm_module in ctx.benchmark_modules:
        run_benchmark(
            ctx.console,
            ctx.sandbox_manager,
            bm_module,
            is_auto=False,
            output_dir=bench_output_dir,
        )


def _cmd_help(_arg: str, ctx: UserCommandContext) -> None:
    lines = [cmd.help for cmd in USER_COMMANDS.values()]
    lines.append("/exit, /quit (also: exit, quit) — end the session")
    ctx.console.print(
        Panel("\n".join(lines), title="Available Commands", border_style="blue")
    )


USER_COMMANDS: Dict[str, UserCommand] = {}
_ALIASES: Dict[str, str] = {}


def _register(command: UserCommand) -> None:
    USER_COMMANDS[command.name] = command
    for token in (command.name,) + command.aliases:
        _ALIASES[token.lower()] = command.name


_register(
    UserCommand(
        name="/todo",
        aliases=(),
        help="/todo <task> — add a TODO item",
        handler=_cmd_todo,
    )
)
_register(
    UserCommand(
        name="/done",
        aliases=(),
        help="/done <id> — mark a TODO item complete",
        handler=_cmd_done,
    )
)
_register(
    UserCommand(
        name="/todos",
        aliases=("todos",),
        help="/todos — list TODO items",
        handler=_cmd_todos,
    )
)
_register(
    UserCommand(
        name="/artifacts",
        aliases=(),
        help="/artifacts — list files produced by this run",
        handler=_cmd_artifacts,
    )
)
_register(
    UserCommand(
        name="/benchmark",
        aliases=("benchmark",),
        help="/benchmark — run the benchmark module(s) configured for this session",
        handler=_cmd_benchmark,
    )
)
_register(
    UserCommand(
        name="/memory",
        aliases=(),
        help="/memory — show the current context/memory usage breakdown",
        handler=_cmd_memory,
    )
)
_register(
    UserCommand(
        name="/evaluate",
        aliases=(),
        help="/evaluate — send the full run context to an evaluator agent for review",
        handler=_cmd_evaluate,
    )
)
_register(
    UserCommand(
        name="/help",
        aliases=(),
        help="/help — list available commands",
        handler=_cmd_help,
    )
)


def _tokenize(raw_input: str) -> Tuple[str, str]:
    stripped = raw_input.strip()
    if not stripped:
        return "", ""
    parts = stripped.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def dispatch_user_command(raw_input: str, ctx: UserCommandContext) -> bool:
    """Exact-match the first token against the command registry and run its
    handler. Returns True if the input was a recognized command (the caller
    should re-prompt), False if it should be treated as a normal chat
    message."""
    first_token, remainder = _tokenize(raw_input)
    canonical = _ALIASES.get(first_token.lower())
    if canonical is None:
        return False
    USER_COMMANDS[canonical].handler(remainder, ctx)
    return True
