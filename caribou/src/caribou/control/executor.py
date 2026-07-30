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
        # The per-launch nonce still distinguishes PID reuse. Keep the fallback
        # stable so parent and child can agree when /proc is unavailable.
        return f"unverified:{pid}"


class LocalProcessExecutor:
    """Launch one worker that survives exit of the submitting CLI process."""

    worker_module = "caribou.control.worker"

    @classmethod
    def _handle(
        cls,
        store: ExperimentStore,
        run_id: str,
        *,
        pid: int,
        launch_nonce: str,
    ) -> ExecutionHandle:
        return ExecutionHandle(
            run_id=run_id,
            pid=pid,
            hostname=socket.gethostname(),
            process_start_identity=_process_start_identity(pid),
            launch_nonce=launch_nonce,
            worker_module=cls.worker_module,
            log_path=str(
                (store.run_dir(run_id) / "worker.log").relative_to(
                    store.run_dir(run_id)
                )
            ),
        )

    @classmethod
    def claim_worker(
        cls, store: ExperimentStore, run_id: str, *, launch_nonce: str
    ) -> bool:
        """Bind the current child before it may execute durable workload actions.

        The parent holds the same mutation lock while spawning and publishing its
        handle. If that parent dies in the Popen-to-write gap, one competing child
        can publish its own identity. Any other child observes a different durable
        handle and exits before changing run state or calling a provider.
        """

        own = cls._handle(
            store,
            run_id,
            pid=os.getpid(),
            launch_nonce=launch_nonce,
        )
        with store.mutation_lock():
            existing = store.execution_handle(run_id)
            if existing is None:
                store.write_execution_handle(own)
                return True
            return (
                existing.run_id == own.run_id
                and existing.pid == own.pid
                and existing.hostname == own.hostname
                and existing.process_start_identity == own.process_start_identity
                and existing.launch_nonce == own.launch_nonce
                and existing.worker_module == own.worker_module
                and existing.log_path == own.log_path
            )

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
            launch_nonce = secrets.token_hex(16)
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
                "--launch-nonce",
                launch_nonce,
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
            handle = self._handle(
                store,
                run_id,
                pid=process.pid,
                launch_nonce=launch_nonce,
            )
            store.write_execution_handle(handle)
            return LaunchResult(handle, True)
