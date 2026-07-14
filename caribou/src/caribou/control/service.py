"""One application service shared by machine CLI and future web adapters."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from caribou.domain.enums import ExecutorKind, RunState
from caribou.domain.models import Artifact, Event, ExperimentSpec, Run

from .api import ControlError, ExitCode
from .executor import LaunchResult, LocalProcessExecutor
from .slurm import ReconciliationResult, SchedulerObservation, SlurmExecutor
from .specs import build_local_plan, load_experiment_spec
from .store import ExperimentStore, Submission


@dataclass(frozen=True)
class SubmittedExperiment:
    submission: Submission
    launches: tuple[LaunchResult, ...]


@dataclass(frozen=True)
class CancellationResult:
    run: Run
    applied: bool
    scheduler_signalled: bool


class ExperimentService:
    """Authoritative local lifecycle operations with transport-free semantics."""

    def __init__(
        self,
        store: ExperimentStore | None = None,
        executor: LocalProcessExecutor | None = None,
        slurm_executor: SlurmExecutor | None = None,
    ) -> None:
        self.store = store or ExperimentStore()
        self.executor = executor or LocalProcessExecutor()
        self.slurm_executor = slurm_executor or SlurmExecutor()

    def validate(self, path: Path) -> ExperimentSpec:
        return load_experiment_spec(path)

    def plan(self, spec: ExperimentSpec) -> dict:
        return build_local_plan(spec)

    def submit(
        self,
        spec: ExperimentSpec,
        *,
        idempotency_key: str,
        expected_plan_hash: str | None = None,
    ) -> SubmittedExperiment:
        plan = self.plan(spec)
        if expected_plan_hash is not None and plan["plan_hash"] != expected_plan_hash:
            raise ControlError(
                "PLAN_CHANGED",
                "the current deterministic plan differs from the expected plan hash",
                exit_code=ExitCode.conflict,
                details={
                    "expected_plan_hash": expected_plan_hash,
                    "current_plan_hash": plan["plan_hash"],
                },
            )
        submission = self.store.submit(spec, idempotency_key)
        selected_executor = (
            self.slurm_executor
            if spec.execution.executor == ExecutorKind.slurm
            else self.executor
        )
        launches = tuple(
            selected_executor.launch(self.store, run.run_id)
            for run in submission.runs
            if run.state.value == "queued"
        )
        if spec.execution.executor == ExecutorKind.slurm:
            # A failed held-job cleanup deliberately leaves the run cancelling.
            # Retrying the same idempotent submit must advance that cleanup rather
            # than returning a misleading no-op success.
            for submitted_run in submission.runs:
                current = self.store.run(submitted_run.run_id)
                if current.state == RunState.cancelling:
                    self.slurm_executor.cancel(self.store, current.run_id)
                    current = self.store.run(current.run_id)
                    if current.state == RunState.cancelled:
                        self.store.reconcile_experiment(current.experiment_id)
        refreshed = Submission(
            experiment=self.store.experiment(submission.experiment.experiment_id),
            runs=tuple(self.store.run(run.run_id) for run in submission.runs),
            plan=submission.plan,
            idempotent_replay=submission.idempotent_replay,
        )
        return SubmittedExperiment(submission=refreshed, launches=launches)

    def status(self, run_id: str) -> Run:
        return self.store.run(run_id)

    def events(self, run_id: str, *, after: int, limit: int) -> tuple[Event, ...]:
        return self.store.events(run_id, after=after, limit=limit)

    def cancel(self, run_id: str, *, reason: str) -> CancellationResult:
        run, applied = self.store.request_cancel(run_id, actor="cli", reason=reason)
        scheduler_signalled = False
        if (
            run.executor == ExecutorKind.slurm
            and run.state == RunState.cancelling
        ):
            scheduler_signalled = self.slurm_executor.cancel(self.store, run_id)
            run = self.store.run(run_id)
            if run.state == RunState.cancelled:
                self.store.reconcile_experiment(run.experiment_id)
        return CancellationResult(
            run=run,
            applied=applied,
            scheduler_signalled=scheduler_signalled,
        )

    def inspect_scheduler(self, run_id: str) -> SchedulerObservation:
        return self.slurm_executor.inspect(self.store, run_id)

    def reconcile_scheduler(self, run_id: str) -> ReconciliationResult:
        return self.slurm_executor.reconcile(self.store, run_id)

    def artifacts(self, run_id: str) -> tuple[Artifact, ...]:
        self.store.run(run_id)
        return self.store.artifact_manifest(run_id).artifacts

    def verify_artifacts(self, run_id: str) -> tuple[Artifact, ...]:
        self.store.run(run_id)
        return self.store.verify_artifacts(run_id)

    def fetch_artifact(
        self,
        run_id: str,
        artifact_id: str,
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> tuple[Artifact, Path]:
        manifest = self.store.artifact_manifest(run_id)
        artifact = manifest.artifact(artifact_id)
        if artifact is None:
            raise ControlError(
                "ARTIFACT_NOT_FOUND",
                f"artifact {artifact_id} is not linked to run {run_id}",
                exit_code=ExitCode.not_found,
            )
        source = self.store.artifact_path(artifact)
        self.store.verify_artifacts(run_id)
        target = Path(os.path.abspath(destination.expanduser()))
        if target.is_dir():
            raise ControlError(
                "OUTPUT_IS_DIRECTORY",
                "artifact destination must be a file path",
                exit_code=ExitCode.conflict,
                details={"output": str(target)},
            )
        if target.is_symlink() or (target.exists() and not overwrite):
            raise ControlError(
                "OUTPUT_EXISTS",
                "artifact destination exists; use --overwrite for a regular file",
                exit_code=ExitCode.conflict,
                details={"output": str(target)},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                for block in iter(lambda: input_file.read(1024 * 1024), b""):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return artifact, target
