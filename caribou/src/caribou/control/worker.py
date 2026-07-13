"""Shared worker entry point used by detached local execution and future Slurm."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from caribou.domain.enums import RunState

from .api import ExitCode
from .specs import (
    ADAPTER_PARAMETER,
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


def _finalize_cancel(store: ExperimentStore, run_id: str) -> None:
    run = store.run(run_id)
    if run.state in TERMINAL_RUN_STATES:
        return
    if run.state != RunState.cancelling:
        store.request_cancel(
            run_id, actor="local-worker", reason="cooperative cancellation observed"
        )
        run = store.run(run_id)
    if run.state == RunState.cancelling:
        store.transition_run(
            run_id,
            RunState.cancelled,
            reason="worker stopped at a cooperative cancellation point",
            actor="local-worker",
            exit_code=int(ExitCode.cancelled),
        )


def execute(store: ExperimentStore, run_id: str) -> int:
    run = store.run(run_id)
    if run.state in TERMINAL_RUN_STATES:
        return 0
    if store.cancel_requested(run_id):
        _finalize_cancel(store, run_id)
        store.reconcile_experiment(run.experiment_id)
        return int(ExitCode.cancelled)
    try:
        if run.state == RunState.queued:
            run, _ = store.transition_run(
                run_id,
                RunState.starting,
                reason="detached worker started",
                actor="local-worker",
            )
        if run.state == RunState.starting:
            run, _ = store.transition_run(
                run_id,
                RunState.running,
                reason="lifecycle smoke workload initialized",
                actor="local-worker",
            )
        condition = _condition(store, run_id)
        adapter = condition.parameters.get(ADAPTER_PARAMETER)
        if adapter != LOCAL_LIFECYCLE_ADAPTER:
            raise RuntimeError(f"unsupported local adapter: {adapter}")
        duration = float(condition.parameters.get(SMOKE_SECONDS_PARAMETER, 0.05))
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
            producer="local-worker",
        )
        store.transition_run(
            run_id,
            RunState.succeeded,
            reason=f"lifecycle smoke artifact {artifact.artifact_id} committed",
            actor="local-worker",
            exit_code=0,
        )
        store.reconcile_experiment(run.experiment_id)
        return 0
    except Exception as exc:
        current = store.run(run_id)
        if current.state not in TERMINAL_RUN_STATES:
            try:
                store.transition_run(
                    run_id,
                    RunState.failed,
                    reason=f"worker failure: {type(exc).__name__}",
                    actor="local-worker",
                    exit_code=int(ExitCode.execution),
                )
            except Exception:
                pass
        try:
            store.reconcile_experiment(run.experiment_id)
        except Exception:
            pass
        print(f"worker failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return int(ExitCode.execution)


def main() -> int:
    parser = argparse.ArgumentParser(description="CARIBOU durable experiment worker")
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args()
    return execute(ExperimentStore(arguments.store_root), arguments.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
