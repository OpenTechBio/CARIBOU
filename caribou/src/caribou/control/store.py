"""Filesystem-backed authoritative store for durable experiment lifecycles."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel

from caribou.config import CARIBOU_HOME
from caribou.domain.enums import (
    ArtifactType,
    EventType,
    ExperimentState,
    InterfaceOrigin,
    RetentionPolicy,
    RunState,
)
from caribou.domain.ids import new_id
from caribou.domain.lifecycle import transition_experiment, transition_run
from caribou.domain.models import (
    Artifact,
    ArtifactCreatedPayload,
    Event,
    Experiment,
    ExperimentSpec,
    Run,
    utc_now,
)
from caribou.domain.serialization import (
    IntegrityError,
    commit_experiment_transition,
    commit_run_event,
    commit_run_transition,
    file_hash,
    initialize_experiment_journal,
    initialize_run_journal,
    model_hash,
    read_experiment_journal,
    read_model,
    read_run_journal,
    sha256_bytes,
    verify_artifact,
    write_model,
)

from .api import ControlError, ExitCode
from .records import (
    ArtifactManifest,
    CancelRequest,
    ExecutionHandle,
    IdempotencyClaim,
    StoreIndex,
)
from .specs import build_local_plan, validate_control_spec


TERMINAL_RUN_STATES = frozenset(
    {
        RunState.succeeded,
        RunState.failed,
        RunState.cancelled,
        RunState.rejected,
        RunState.resumable,
    }
)


def _validate_path_identifier(value: str, kind: str) -> str:
    if re.fullmatch(rf"{kind}_[0-9a-f]{{32}}", value) is None:
        raise ControlError(
            f"{kind.upper()}_ID_INVALID",
            f"{kind} identifier is not a canonical CARIBOU ID",
            exit_code=ExitCode.validation,
            details={"identifier": value},
        )
    return value


@dataclass(frozen=True)
class Submission:
    experiment: Experiment
    runs: tuple[Run, ...]
    plan: dict
    idempotent_replay: bool


def default_store_root() -> Path:
    configured = os.environ.get("CARIBOU_EXPERIMENT_HOME")
    return (
        Path(configured).expanduser()
        if configured
        else CARIBOU_HOME / "experiment_store" / "v1"
    )


class ExperimentStore:
    """One low-inode, crash-safe local control-plane store."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or default_store_root()).expanduser().resolve()

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def experiment_dir(self, experiment_id: str) -> Path:
        return (
            self.root / "experiments" / _validate_path_identifier(experiment_id, "exp")
        )

    def experiment_journal_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / "experiment-journal.json"

    def experiment_spec_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / "spec.json"

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / _validate_path_identifier(run_id, "run")

    def run_journal_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run-journal.json"

    def artifact_manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts.json"

    def execution_handle_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "executor.json"

    def cancel_request_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "control.json"

    @contextmanager
    def mutation_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.root / ".caribou-control.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_index_unlocked(self) -> StoreIndex:
        if not self.index_path.exists():
            return StoreIndex()
        return self._read(self.index_path, StoreIndex)

    def index(self) -> StoreIndex:
        if not self.index_path.is_file():
            raise ControlError(
                "STORE_NOT_FOUND",
                "the experiment store has not been initialized",
                exit_code=ExitCode.not_found,
                details={"root": str(self.root)},
            )
        return self._read(self.index_path, StoreIndex)

    def _read(self, path: Path, model_type: type[BaseModel]):
        if not path.is_file():
            raise ControlError(
                "RECORD_NOT_FOUND",
                f"durable record does not exist: {path.name}",
                exit_code=ExitCode.not_found,
                details={"path": str(path)},
            )
        try:
            return read_model(path, model_type)
        except IntegrityError as exc:
            raise ControlError(
                "STORE_INTEGRITY_ERROR",
                "a durable control-plane record failed validation",
                exit_code=ExitCode.integrity,
                details={"path": str(path), "reason": str(exc)},
            ) from exc

    @staticmethod
    def _idempotency_hash(key: str) -> str:
        return sha256_bytes(key.encode("utf-8"))

    @staticmethod
    def _validated_run(run: Run, updates: dict) -> Run:
        candidate = run.model_copy(update=updates)
        return Run.model_validate_json(candidate.model_dump_json())

    def submit(self, spec: ExperimentSpec, idempotency_key: str) -> Submission:
        """Atomically claim an idempotency key and create the complete run matrix."""

        if not idempotency_key.strip():
            raise ControlError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "submission requires a non-empty idempotency key",
                exit_code=ExitCode.validation,
            )
        if idempotency_key != idempotency_key.strip() or len(idempotency_key) > 256:
            raise ControlError(
                "IDEMPOTENCY_KEY_INVALID",
                "idempotency key must be trimmed and at most 256 characters",
                exit_code=ExitCode.validation,
            )
        validate_control_spec(spec, require_local_adapter=True)
        plan = build_local_plan(spec)
        spec_hash = model_hash(spec)
        key_hash = self._idempotency_hash(idempotency_key)
        with self.mutation_lock():
            index = self._read_index_unlocked()
            existing = index.idempotency.get(key_hash)
            if existing is not None:
                if existing.spec_hash != spec_hash:
                    raise ControlError(
                        "IDEMPOTENCY_CONFLICT",
                        "the idempotency key is already bound to another specification",
                        exit_code=ExitCode.conflict,
                        details={
                            "experiment_id": existing.experiment_id,
                            "existing_spec_hash": existing.spec_hash,
                            "submitted_spec_hash": spec_hash,
                        },
                    )
                experiment = self.experiment(existing.experiment_id)
                existing_runs = tuple(self.run(run_id) for run_id in existing.run_ids)
                return Submission(experiment, existing_runs, plan, True)

            now = utc_now()
            experiment_id = new_id("exp")
            new_runs: list[Run] = []
            for condition in spec.conditions:
                for replicate_index in range(spec.repetitions):
                    run = Run(
                        experiment_id=experiment_id,
                        spec_hash=spec_hash,
                        condition_id=condition.condition_id,
                        replicate_index=replicate_index,
                        idempotency_key=(
                            f"{idempotency_key}:{condition.condition_id}:"
                            f"{replicate_index}:1"
                        ),
                        interface=InterfaceOrigin.cli,
                        owner=spec.owner,
                        initial_state=RunState.planned,
                        state=RunState.planned,
                        executor=spec.execution.executor,
                        code=spec.code,
                        resolved_model=condition.model,
                        resolved_blueprint=condition.blueprint,
                        resolved_prompt=condition.prompt,
                        resolved_memory=condition.memory,
                        resolved_inputs=list(spec.inputs),
                        resolved_stop_rules=spec.stop_rules,
                        resolved_budget=spec.budget,
                        container=spec.execution.container,
                        resources=spec.execution.resources,
                        partition=spec.execution.partition,
                        created_at=now,
                        updated_at=now,
                    )
                    new_runs.append(run)
            experiment = Experiment(
                experiment_id=experiment_id,
                spec_id=spec.spec_id,
                spec_version=spec.spec_version,
                spec_hash=spec_hash,
                owner=spec.owner,
                run_ids=[run.run_id for run in new_runs],
                created_at=now,
                updated_at=now,
            )

            experiment_directory = self.experiment_dir(experiment_id)
            experiment_directory.mkdir(parents=True, mode=0o700)
            write_model(self.experiment_spec_path(experiment_id), spec)
            experiment_hash = initialize_experiment_journal(
                self.experiment_journal_path(experiment_id), experiment
            )
            for target, reason in (
                (ExperimentState.validated, "specification validated"),
                (ExperimentState.planned, "run matrix planned"),
                (ExperimentState.active, "local workers authorized"),
            ):
                experiment_transition = transition_experiment(
                    experiment, target, reason=reason, actor="control-plane"
                )
                experiment_hash = commit_experiment_transition(
                    self.experiment_journal_path(experiment_id),
                    experiment_transition,
                    expected_hash=experiment_hash,
                )
                experiment = experiment_transition.experiment

            queued_runs: list[Run] = []
            for run in new_runs:
                run_directory = self.run_dir(run.run_id)
                run_directory.mkdir(parents=True, mode=0o700)
                run_hash = initialize_run_journal(
                    self.run_journal_path(run.run_id), run
                )
                write_model(
                    self.artifact_manifest_path(run.run_id),
                    ArtifactManifest(run_id=run.run_id),
                )
                run_transition = transition_run(
                    run,
                    RunState.queued,
                    reason="accepted by local executor",
                    actor="control-plane",
                )
                commit_run_transition(
                    self.run_journal_path(run.run_id),
                    run_transition,
                    expected_hash=run_hash,
                )
                queued_runs.append(run_transition.run)

            experiments = dict(index.experiments)
            experiments[experiment_id] = tuple(run.run_id for run in queued_runs)
            run_index = dict(index.runs)
            run_index.update({run.run_id: experiment_id for run in queued_runs})
            claims = dict(index.idempotency)
            claims[key_hash] = IdempotencyClaim(
                spec_hash=spec_hash,
                experiment_id=experiment_id,
                run_ids=tuple(run.run_id for run in queued_runs),
                created_at=now,
            )
            updated_index = StoreIndex(
                experiments=experiments,
                runs=run_index,
                idempotency=claims,
                updated_at=utc_now(),
            )
            write_model(
                self.index_path,
                updated_index,
                expected_hash=file_hash(self.index_path)
                if self.index_path.exists()
                else None,
            )
            return Submission(experiment, tuple(queued_runs), plan, False)

    def experiment(self, experiment_id: str) -> Experiment:
        try:
            return read_experiment_journal(
                self.experiment_journal_path(experiment_id)
            ).experiment
        except IntegrityError as exc:
            if not self.experiment_journal_path(experiment_id).exists():
                raise ControlError(
                    "EXPERIMENT_NOT_FOUND",
                    f"unknown experiment: {experiment_id}",
                    exit_code=ExitCode.not_found,
                ) from exc
            raise ControlError(
                "STORE_INTEGRITY_ERROR",
                "experiment journal failed validation",
                exit_code=ExitCode.integrity,
                details={"experiment_id": experiment_id, "reason": str(exc)},
            ) from exc

    def spec(self, experiment_id: str) -> ExperimentSpec:
        return self._read(self.experiment_spec_path(experiment_id), ExperimentSpec)

    def run(self, run_id: str) -> Run:
        try:
            return read_run_journal(self.run_journal_path(run_id)).run
        except IntegrityError as exc:
            if not self.run_journal_path(run_id).exists():
                raise ControlError(
                    "RUN_NOT_FOUND",
                    f"unknown run: {run_id}",
                    exit_code=ExitCode.not_found,
                ) from exc
            raise ControlError(
                "STORE_INTEGRITY_ERROR",
                "run journal failed validation",
                exit_code=ExitCode.integrity,
                details={"run_id": run_id, "reason": str(exc)},
            ) from exc

    def experiment_id_for_run(self, run_id: str) -> str:
        experiment_id = self.index().runs.get(run_id)
        if experiment_id is None:
            raise ControlError(
                "RUN_NOT_FOUND",
                f"unknown run: {run_id}",
                exit_code=ExitCode.not_found,
            )
        return experiment_id

    def events(
        self, run_id: str, *, after: int = 0, limit: int = 1000
    ) -> tuple[Event, ...]:
        if after < 0 or limit < 1 or limit > 10000:
            raise ControlError(
                "CURSOR_INVALID",
                "after must be nonnegative and limit must be between 1 and 10000",
                exit_code=ExitCode.validation,
            )
        self.run(run_id)
        journal = read_run_journal(self.run_journal_path(run_id))
        return tuple(event for event in journal.events if event.sequence > after)[
            :limit
        ]

    def _transition_run_unlocked(
        self,
        run_id: str,
        target: RunState,
        *,
        reason: str,
        actor: str,
        exit_code: Optional[int] = None,
    ) -> tuple[Run, bool]:
        path = self.run_journal_path(run_id)
        journal = read_run_journal(path)
        transition = transition_run(
            journal.run,
            target,
            reason=reason,
            actor=actor,
            exit_code=exit_code,
        )
        if not transition.applied:
            return transition.run, False
        commit_run_transition(path, transition, expected_hash=file_hash(path))
        return transition.run, True

    def transition_run(
        self,
        run_id: str,
        target: RunState,
        *,
        reason: str,
        actor: str,
        exit_code: Optional[int] = None,
    ) -> tuple[Run, bool]:
        with self.mutation_lock():
            return self._transition_run_unlocked(
                run_id,
                target,
                reason=reason,
                actor=actor,
                exit_code=exit_code,
            )

    def request_cancel(
        self, run_id: str, *, actor: str, reason: str
    ) -> tuple[Run, bool]:
        with self.mutation_lock():
            run = self.run(run_id)
            if run.state in TERMINAL_RUN_STATES:
                return run, False
            request_path = self.cancel_request_path(run_id)
            if not request_path.exists():
                write_model(
                    request_path,
                    CancelRequest(run_id=run_id, actor=actor, reason=reason),
                )
            if run.state == RunState.cancelling:
                return run, False
            target = (
                RunState.cancelled
                if run.state in {RunState.draft, RunState.validated, RunState.planned}
                else RunState.cancelling
            )
            return self._transition_run_unlocked(
                run_id, target, reason=reason, actor=actor
            )

    def cancel_requested(self, run_id: str) -> bool:
        return self.cancel_request_path(run_id).is_file()

    def artifact_manifest(self, run_id: str) -> ArtifactManifest:
        return self._read(self.artifact_manifest_path(run_id), ArtifactManifest)

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def record_json_artifact(
        self,
        run_id: str,
        *,
        filename: str,
        role: str,
        value: dict,
        producer: str,
    ) -> Artifact:
        if filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ControlError(
                "ARTIFACT_FILENAME_INVALID",
                "artifact filename must be one path-safe component",
                exit_code=ExitCode.validation,
                details={"filename": filename},
            )
        with self.mutation_lock():
            journal_path = self.run_journal_path(run_id)
            journal = read_run_journal(journal_path)
            if journal.run.state not in {RunState.running, RunState.checkpointed}:
                raise ControlError(
                    "RUN_NOT_WRITABLE",
                    "artifacts can be registered only while an attempt is running",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": journal.run.state.value},
                )
            data = (
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            artifact_id = new_id("art")
            event_id = new_id("evt")
            storage_name = f"{artifact_id}-{filename}"
            storage_uri = f"artifacts/{storage_name}"
            artifact_path = self.run_dir(run_id) / storage_uri
            self._atomic_bytes(artifact_path, data)
            timestamp = utc_now()
            event = Event(
                event_id=event_id,
                experiment_id=journal.run.experiment_id,
                run_id=run_id,
                sequence=journal.run.event_sequence + 1,
                occurred_at=timestamp,
                event_type=EventType.artifact_created,
                turn=journal.run.current_turn,
                actor=producer,
                payload=ArtifactCreatedPayload(artifact_id=artifact_id),
            )
            artifact = Artifact(
                artifact_id=artifact_id,
                experiment_id=journal.run.experiment_id,
                run_id=run_id,
                producer_event_id=event_id,
                producer=producer,
                artifact_type=ArtifactType.manifest,
                role=role,
                filename=filename,
                storage_uri=storage_uri,
                content_hash=sha256_bytes(data),
                media_type="application/json",
                schema_type="caribou.lifecycle_smoke_result",
                schema_version_name="v1",
                size_bytes=len(data),
                created_at=timestamp,
                retention=RetentionPolicy.experiment,
                owner=journal.run.owner,
            )
            manifest_path = self.artifact_manifest_path(run_id)
            manifest = self.artifact_manifest(run_id)
            updated_manifest = ArtifactManifest(
                run_id=run_id,
                artifacts=(*manifest.artifacts, artifact),
                updated_at=timestamp,
            )
            write_model(
                manifest_path,
                updated_manifest,
                expected_hash=file_hash(manifest_path),
            )
            updated_run = self._validated_run(
                journal.run,
                {
                    "artifact_ids": [*journal.run.artifact_ids, artifact_id],
                    "event_sequence": event.sequence,
                    "updated_at": timestamp,
                },
            )
            commit_run_event(
                journal_path,
                updated_run,
                event,
                expected_hash=file_hash(journal_path),
            )
            return artifact

    def artifact_path(self, artifact: Artifact) -> Path:
        path = self.run_dir(artifact.run_id) / artifact.storage_uri
        try:
            if path.is_symlink():
                raise ValueError("artifact is a symlink")
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.run_dir(artifact.run_id).resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ControlError(
                "ARTIFACT_PATH_INVALID",
                "artifact storage path escapes its run root or is missing",
                exit_code=ExitCode.integrity,
                details={"artifact_id": artifact.artifact_id},
            ) from exc
        return path

    def verify_artifacts(self, run_id: str) -> tuple[Artifact, ...]:
        manifest = self.artifact_manifest(run_id)
        for artifact in manifest.artifacts:
            try:
                verify_artifact(
                    self.artifact_path(artifact),
                    artifact.content_hash,
                    artifact.size_bytes,
                    root=self.run_dir(run_id),
                )
            except IntegrityError as exc:
                raise ControlError(
                    "ARTIFACT_INTEGRITY_ERROR",
                    "artifact content failed hash, size, or path validation",
                    exit_code=ExitCode.integrity,
                    details={
                        "artifact_id": artifact.artifact_id,
                        "reason": str(exc),
                    },
                ) from exc
        return manifest.artifacts

    def execution_handle(self, run_id: str) -> Optional[ExecutionHandle]:
        path = self.execution_handle_path(run_id)
        return self._read(path, ExecutionHandle) if path.exists() else None

    def write_execution_handle(self, handle: ExecutionHandle) -> None:
        path = self.execution_handle_path(handle.run_id)
        if path.exists():
            raise ControlError(
                "WORKER_ALREADY_LAUNCHED",
                "an execution handle already exists for this run",
                exit_code=ExitCode.conflict,
                details={"run_id": handle.run_id},
            )
        write_model(path, handle)

    def reconcile_experiment(self, experiment_id: str) -> Experiment:
        with self.mutation_lock():
            index = self._read_index_unlocked()
            run_ids = index.experiments.get(experiment_id)
            if run_ids is None:
                raise ControlError(
                    "EXPERIMENT_NOT_FOUND",
                    f"unknown experiment: {experiment_id}",
                    exit_code=ExitCode.not_found,
                )
            runs = [self.run(run_id) for run_id in run_ids]
            experiment_path = self.experiment_journal_path(experiment_id)
            journal = read_experiment_journal(experiment_path)
            if journal.experiment.state != ExperimentState.active:
                return journal.experiment
            if not all(run.state in TERMINAL_RUN_STATES for run in runs):
                return journal.experiment
            if all(run.state == RunState.succeeded for run in runs):
                first = transition_experiment(
                    journal.experiment,
                    ExperimentState.aggregating,
                    reason="all attempts reached a successful terminal state",
                    actor="control-plane",
                )
                first_hash = commit_experiment_transition(
                    experiment_path, first, expected_hash=file_hash(experiment_path)
                )
                second = transition_experiment(
                    first.experiment,
                    ExperimentState.completed,
                    reason="no aggregate was required for the lifecycle probe",
                    actor="control-plane",
                )
                commit_experiment_transition(
                    experiment_path, second, expected_hash=first_hash
                )
                return second.experiment
            target = (
                ExperimentState.failed
                if any(
                    run.state in {RunState.failed, RunState.rejected} for run in runs
                )
                else ExperimentState.cancelled
            )
            transition = transition_experiment(
                journal.experiment,
                target,
                reason="one or more attempts ended without success",
                actor="control-plane",
            )
            commit_experiment_transition(
                experiment_path,
                transition,
                expected_hash=file_hash(experiment_path),
            )
            return transition.experiment
