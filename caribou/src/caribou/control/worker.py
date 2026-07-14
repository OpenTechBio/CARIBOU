"""Shared worker entry point used by detached local execution and future Slurm."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from caribou.domain.enums import ExecutorKind, RunState
from caribou.domain.models import Run

from .api import ControlError, ExitCode
from .specs import (
    ADAPTER_PARAMETER,
    AGENT_PATH_SMOKE_ADAPTER,
    CARIBOU_AGENT_ADAPTER,
    LOCAL_LIFECYCLE_ADAPTER,
    SMOKE_SECONDS_PARAMETER,
)
from .store import ExperimentStore, TERMINAL_RUN_STATES


def _condition(store: ExperimentStore, run_id: str):
    run = store.run(run_id)
    spec = store.spec(run.experiment_id)
    return next(
        item for item in spec.conditions if item.condition_id == run.condition_id
    )


def _worker_actor(run: Run) -> str:
    return "slurm-worker" if run.executor == ExecutorKind.slurm else "local-worker"


def _validate_execution_context(run: Run) -> None:
    if run.executor != ExecutorKind.slurm:
        return
    job_id = os.environ.get("SLURM_JOB_ID")
    partition = os.environ.get("SLURM_JOB_PARTITION")
    if job_id != run.scheduler_job_id or partition != run.partition:
        raise ControlError(
            "SLURM_CONTEXT_MISMATCH",
            "the worker Slurm context differs from the durable run binding",
            exit_code=ExitCode.integrity,
            details={
                "run_id": run.run_id,
                "expected_job_id": run.scheduler_job_id,
                "observed_job_id": job_id,
                "expected_partition": run.partition,
                "observed_partition": partition,
            },
        )


def _finalize_cancel(store: ExperimentStore, run_id: str) -> None:
    run = store.run(run_id)
    actor = _worker_actor(run)
    if run.state in TERMINAL_RUN_STATES:
        return
    if run.state != RunState.cancelling:
        store.request_cancel(
            run_id, actor=actor, reason="cooperative cancellation observed"
        )
        run = store.run(run_id)
    if run.state == RunState.cancelling:
        store.transition_run(
            run_id,
            RunState.cancelled,
            reason="worker stopped at a cooperative cancellation point",
            actor=actor,
            exit_code=int(ExitCode.cancelled),
        )


def execute(store: ExperimentStore, run_id: str) -> int:
    run = store.run(run_id)
    if run.state in TERMINAL_RUN_STATES:
        return 0
    actor = _worker_actor(run)
    try:
        _validate_execution_context(run)
        if store.cancel_requested(run_id):
            _finalize_cancel(store, run_id)
            store.reconcile_experiment(run.experiment_id)
            return int(ExitCode.cancelled)
        if run.state == RunState.queued:
            run, _ = store.transition_run(
                run_id,
                RunState.starting,
                reason="detached worker started",
                actor=actor,
            )
        condition = _condition(store, run_id)
        adapter = condition.parameters.get(ADAPTER_PARAMETER)
        if adapter == LOCAL_LIFECYCLE_ADAPTER:
            if run.state == RunState.starting:
                run, _ = store.transition_run(
                    run_id,
                    RunState.running,
                    reason="lifecycle smoke workload initialized",
                    actor=actor,
                )
            duration = float(
                condition.parameters.get(SMOKE_SECONDS_PARAMETER, 0.05)
            )
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if store.cancel_requested(run_id):
                    _finalize_cancel(store, run_id)
                    store.reconcile_experiment(run.experiment_id)
                    return int(ExitCode.cancelled)
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            current = store.run(run_id)
            if current.state == RunState.cancelling or store.cancel_requested(run_id):
                _finalize_cancel(store, run_id)
                store.reconcile_experiment(run.experiment_id)
                return int(ExitCode.cancelled)
            artifact = store.record_json_artifact(
                run_id,
                filename="lifecycle-smoke-result.json",
                role="lifecycle_smoke_result",
                value={
                    "schema_version": "caribou.lifecycle_smoke_result.v1",
                    "run_id": run_id,
                    "condition_id": current.condition_id,
                    "replicate_index": current.replicate_index,
                    "status": "completed",
                },
                producer=actor,
            )
            reason = f"lifecycle smoke artifact {artifact.artifact_id} committed"
        elif adapter in {AGENT_PATH_SMOKE_ADAPTER, CARIBOU_AGENT_ADAPTER}:
            from .agent_workload import execute_agent_workload

            result = execute_agent_workload(
                store,
                run_id,
                adapter=str(adapter),
                actor=actor,
            )
            if result is None or result.cancelled or store.cancel_requested(run_id):
                _finalize_cancel(store, run_id)
                store.reconcile_experiment(run.experiment_id)
                return int(ExitCode.cancelled)
            if not result.succeeded:
                store.transition_run(
                    run_id,
                    RunState.failed,
                    reason=f"agent session failed: {result.end_reason}",
                    actor=actor,
                    exit_code=int(ExitCode.execution),
                )
                store.reconcile_experiment(run.experiment_id)
                return int(ExitCode.execution)
            reason = f"agent session completed: {result.end_reason}"
        else:
            raise RuntimeError(f"unsupported local adapter: {adapter}")
        store.transition_run(
            run_id,
            RunState.succeeded,
            reason=reason,
            actor=actor,
            exit_code=0,
        )
        store.reconcile_experiment(run.experiment_id)
        return 0
    except Exception as exc:
        failure_code = exc.code if isinstance(exc, ControlError) else type(exc).__name__
        current = store.run(run_id)
        if current.state == RunState.cancelling or store.cancel_requested(run_id):
            _finalize_cancel(store, run_id)
            store.reconcile_experiment(run.experiment_id)
            return int(ExitCode.cancelled)
        if current.state not in TERMINAL_RUN_STATES:
            try:
                store.transition_run(
                    run_id,
                    RunState.failed,
                    reason=f"worker failure: {failure_code}",
                    actor=actor,
                    exit_code=int(ExitCode.execution),
                )
            except Exception:
                pass
        try:
            store.reconcile_experiment(run.experiment_id)
        except Exception:
            pass
        print(f"worker failure: {failure_code}: {exc}", file=sys.stderr)
        return int(ExitCode.execution)


def main() -> int:
    parser = argparse.ArgumentParser(description="CARIBOU durable experiment worker")
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args()
    return execute(ExperimentStore(arguments.store_root), arguments.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
