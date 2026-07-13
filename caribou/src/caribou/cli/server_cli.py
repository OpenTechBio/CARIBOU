from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, List

import typer


server_app = typer.Typer(
    name="server",
    help="Manage CARIBOU web server processes.",
    no_args_is_help=True,
)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command: str


def _is_caribou_server_process(command: str) -> bool:
    parts = command.split()
    if not parts:
        return False

    for idx, part in enumerate(parts):
        if os.path.basename(part) == "caribou" and idx + 1 < len(parts) and parts[idx + 1] == "serve":
            return True

    if any(part == "caribou.cli.main" for part in parts) and "serve" in parts:
        return True

    return any(os.path.basename(part) == "uvicorn" for part in parts) and "caribou.server.main:app" in command


def _list_user_processes() -> List[ProcessInfo]:
    user = os.environ.get("USER")
    cmd = ["ps", "-o", "pid=", "-o", "command="]
    if user:
        cmd[1:1] = ["-u", user]

    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    processes: List[ProcessInfo] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append(ProcessInfo(pid=pid, command=command.strip()))
    return processes


def _find_server_processes(processes: Iterable[ProcessInfo] | None = None) -> List[ProcessInfo]:
    current_pid = os.getpid()
    candidates = processes if processes is not None else _list_user_processes()
    return [
        proc for proc in candidates
        if proc.pid != current_pid and _is_caribou_server_process(proc.command)
    ]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(processes: Iterable[ProcessInfo], timeout: float) -> List[ProcessInfo]:
    deadline = time.monotonic() + timeout
    remaining = list(processes)
    while remaining and time.monotonic() < deadline:
        remaining = [proc for proc in remaining if _process_alive(proc.pid)]
        if remaining:
            time.sleep(0.2)
    return [proc for proc in remaining if _process_alive(proc.pid)]


def _signal_processes(processes: Iterable[ProcessInfo], sig: signal.Signals) -> List[ProcessInfo]:
    failed: List[ProcessInfo] = []
    for proc in processes:
        try:
            os.kill(proc.pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            failed.append(proc)
    return failed


@server_app.command("cleanup")
def cleanup(
    dry_run: bool = typer.Option(False, "--dry-run", help="List matching server processes without terminating them."),
    force: bool = typer.Option(False, "--force", help="Send SIGKILL to processes still alive after SIGTERM."),
    wait_seconds: float = typer.Option(5.0, "--wait-seconds", min=0.1, help="Seconds to wait after SIGTERM."),
) -> None:
    """
    Terminate stale CARIBOU web server processes for the current user.
    """
    processes = _find_server_processes()
    if not processes:
        typer.echo("No CARIBOU server processes found.")
        return

    typer.echo("Matched CARIBOU server processes:")
    for proc in processes:
        typer.echo(f"  {proc.pid}: {proc.command}")

    if dry_run:
        typer.echo("Dry run only; no processes were signaled.")
        return

    failed = _signal_processes(processes, signal.SIGTERM)
    if failed:
        typer.echo("Could not signal some processes:")
        for proc in failed:
            typer.echo(f"  {proc.pid}: permission denied")

    remaining = _wait_for_exit(processes, wait_seconds)
    if remaining and force:
        typer.echo("Some processes are still running; sending SIGKILL because --force was set.")
        _signal_processes(remaining, signal.SIGKILL)
        remaining = _wait_for_exit(remaining, 2.0)

    if remaining:
        typer.echo("Some processes are still running:")
        for proc in remaining:
            typer.echo(f"  {proc.pid}: {proc.command}")
        raise typer.Exit(1)

    typer.echo("CARIBOU server cleanup complete.")
