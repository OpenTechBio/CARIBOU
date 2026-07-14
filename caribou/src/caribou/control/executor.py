"""Detached local transport for the shared CARIBOU worker entry point."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .api import ControlError, ExitCode
from .records import ExecutionHandle, SlurmExecutionHandle
from .store import ExperimentStore, TERMINAL_RUN_STATES


@dataclass(frozen=True)
class LaunchResult:
    handle: ExecutionHandle | SlurmExecutionHandle
    launched: bool


def _process_start_identity(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return f"linux-proc:{pid}:{fields[21]}"
    except (OSError, IndexError):
        return f"unverified:{pid}:{secrets.token_hex(16)}"


class LocalProcessExecutor:
    """Launch one worker that survives exit of the submitting CLI process."""

    worker_module = "caribou.control.worker"

    def launch(self, store: ExperimentStore, run_id: str) -> LaunchResult:
        with store.mutation_lock():
            existing = store.execution_handle(run_id)
            if existing is not None:
                return LaunchResult(existing, False)
            run = store.run(run_id)
            if run.state in TERMINAL_RUN_STATES:
                raise ControlError(
                    "RUN_TERMINAL",
                    "a terminal run cannot launch a worker",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": run.state.value},
                )
            if run.state.value != "queued":
                raise ControlError(
                    "RUN_NOT_QUEUED",
                    "the local executor launches only a durably queued run",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": run.state.value},
                )
            log_path = store.run_dir(run_id) / "worker.log"
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONNOUSERSITE": "1",
                    "CARIBOU_EXPERIMENT_HOME": str(store.root),
                }
            )
            command = [
                sys.executable,
                "-m",
                self.worker_module,
                "--store-root",
                str(store.root),
                "--run-id",
                run_id,
            ]
            with log_path.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            handle = ExecutionHandle(
                run_id=run_id,
                pid=process.pid,
                hostname=socket.gethostname(),
                process_start_identity=_process_start_identity(process.pid),
                launch_nonce=secrets.token_hex(16),
                worker_module=self.worker_module,
                log_path=str(log_path.relative_to(store.run_dir(run_id))),
            )
            store.write_execution_handle(handle)
            return LaunchResult(handle, True)
