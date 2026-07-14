"""Filesystem-backed authoritative store for durable experiment lifecycles."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
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
    ExecutorKind,
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
    EventPayload,
    Experiment,
    ExperimentSpec,
    HeartbeatPayload,
    Run,
    utc_now,
)
from caribou.domain.serialization import (
    IntegrityError,
    canonical_json_bytes,
    commit_experiment_transition,
    commit_run_event,
    commit_run_scheduler_binding,
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
    SlurmAccounting,
    SlurmCancellationAttempt,
    SlurmCancellationLedger,
    SlurmExecutionHandle,
    SlurmSubmissionLedger,
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

    def scheduler_handle_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "scheduler.json"

    def scheduler_accounting_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "scheduler-accounting.json"

    def scheduler_accounting_raw_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "scheduler-accounting.raw"

    def scheduler_script_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "slurm-job.sh"

    def scheduler_cancellation_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "scheduler-cancellation.json"

    def scheduler_submission_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "scheduler-submission.json"

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
        validate_control_spec(spec, require_submit_adapter=True)
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
            executor_label = spec.execution.executor.value
            for target, reason in (
                (ExperimentState.validated, "specification validated"),
                (ExperimentState.planned, "run matrix planned"),
                (ExperimentState.active, f"{executor_label} workers authorized"),
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
                    reason=f"accepted by {executor_label} executor",
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

    def append_run_event(
        self,
        run_id: str,
        *,
        event_type: EventType,
        payload: EventPayload,
        actor: str,
        turn: int,
        current_agent: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> Event:
        """Append one typed durable runner event to the authoritative journal."""

        if event_type in {EventType.state_transition, EventType.token}:
            raise ControlError(
                "EVENT_TYPE_UNSUPPORTED",
                "runner events cannot append transitions or ephemeral tokens",
                exit_code=ExitCode.validation,
                details={"event_type": event_type.value},
            )
        with self.mutation_lock():
            path = self.run_journal_path(run_id)
            journal = read_run_journal(path)
            if journal.run.state not in {
                RunState.running,
                RunState.checkpointed,
                RunState.cancelling,
            }:
                raise ControlError(
                    "RUN_NOT_EVENT_WRITABLE",
                    "runner events require an active attempt",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": journal.run.state.value},
                )
            if turn < journal.run.current_turn:
                raise ControlError(
                    "EVENT_TURN_REGRESSION",
                    "runner event turn cannot move backward",
                    exit_code=ExitCode.integrity,
                    details={
                        "run_id": run_id,
                        "current_turn": journal.run.current_turn,
                        "event_turn": turn,
                    },
                )
            timestamp = utc_now()
            event = Event(
                experiment_id=journal.run.experiment_id,
                run_id=run_id,
                sequence=journal.run.event_sequence + 1,
                occurred_at=timestamp,
                event_type=event_type,
                turn=turn,
                stage=stage,
                actor=actor,
                payload=payload,
            )
            updates: dict = {
                "event_sequence": event.sequence,
                "current_turn": turn,
                "updated_at": timestamp,
            }
            if current_agent is not None:
                updates["current_agent"] = current_agent
            updated_run = self._validated_run(journal.run, updates)
            commit_run_event(
                path,
                updated_run,
                event,
                expected_hash=file_hash(path),
            )
            return event

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
        artifact_type: ArtifactType = ArtifactType.manifest,
        media_type: str = "application/json",
        schema_type: Optional[str] = "caribou.lifecycle_smoke_result",
        schema_version_name: Optional[str] = "v1",
        turn: Optional[int] = None,
        current_agent: Optional[str] = None,
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
            if journal.run.state not in {
                RunState.running,
                RunState.checkpointed,
                RunState.cancelling,
            }:
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
            return self._record_artifact_bytes_unlocked(
                journal_path=journal_path,
                journal_run=journal.run,
                filename=filename,
                role=role,
                data=data,
                producer=producer,
                artifact_type=artifact_type,
                media_type=media_type,
                schema_type=schema_type,
                schema_version_name=schema_version_name,
                turn=turn,
                current_agent=current_agent,
            )

    def record_text_artifact(
        self,
        run_id: str,
        *,
        filename: str,
        role: str,
        text: str,
        producer: str,
        artifact_type: ArtifactType,
        media_type: str = "text/plain",
        turn: Optional[int] = None,
        current_agent: Optional[str] = None,
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
            if journal.run.state not in {
                RunState.running,
                RunState.checkpointed,
                RunState.cancelling,
            }:
                raise ControlError(
                    "RUN_NOT_WRITABLE",
                    "artifacts can be registered only while an attempt is running",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": journal.run.state.value},
                )
            return self._record_artifact_bytes_unlocked(
                journal_path=journal_path,
                journal_run=journal.run,
                filename=filename,
                role=role,
                data=text.encode("utf-8"),
                producer=producer,
                artifact_type=artifact_type,
                media_type=media_type,
                schema_type=None,
                schema_version_name=None,
                turn=turn,
                current_agent=current_agent,
            )

    def record_file_artifact(
        self,
        run_id: str,
        *,
        source: Path,
        filename: str,
        role: str,
        producer: str,
        artifact_type: ArtifactType,
        media_type: str,
        turn: Optional[int] = None,
        current_agent: Optional[str] = None,
    ) -> Artifact:
        """Copy one regular generated file into immutable run artifact storage."""

        if filename in {".", ".."} or "/" in filename or "\\" in filename:
            raise ControlError(
                "ARTIFACT_FILENAME_INVALID",
                "artifact filename must be one path-safe component",
                exit_code=ExitCode.validation,
                details={"filename": filename},
            )
        candidate = source.expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise ControlError(
                "ARTIFACT_SOURCE_INVALID",
                "artifact source must be a regular non-symlink file",
                exit_code=ExitCode.integrity,
                details={"source": str(candidate)},
            )
        with self.mutation_lock():
            journal_path = self.run_journal_path(run_id)
            journal = read_run_journal(journal_path)
            if journal.run.state not in {
                RunState.running,
                RunState.checkpointed,
                RunState.cancelling,
            }:
                raise ControlError(
                    "RUN_NOT_WRITABLE",
                    "artifacts can be registered only while an attempt is running",
                    exit_code=ExitCode.conflict,
                    details={"run_id": run_id, "state": journal.run.state.value},
                )
            artifact_id = new_id("art")
            storage_uri = f"artifacts/{artifact_id}-{filename}"
            destination = self.run_dir(run_id) / storage_uri
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    candidate.open("rb") as input_file,
                ):
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            return self._commit_artifact_unlocked(
                journal_path=journal_path,
                journal_run=journal.run,
                artifact_id=artifact_id,
                filename=filename,
                storage_uri=storage_uri,
                role=role,
                producer=producer,
                artifact_type=artifact_type,
                media_type=media_type,
                schema_type=None,
                schema_version_name=None,
                content_hash=file_hash(destination),
                size_bytes=destination.stat().st_size,
                turn=turn,
                current_agent=current_agent,
            )

    def _record_artifact_bytes_unlocked(
        self,
        *,
        journal_path: Path,
        journal_run: Run,
        filename: str,
        role: str,
        data: bytes,
        producer: str,
        artifact_type: ArtifactType,
        media_type: str,
        schema_type: Optional[str],
        schema_version_name: Optional[str],
        turn: Optional[int],
        current_agent: Optional[str],
    ) -> Artifact:
        artifact_id = new_id("art")
        storage_uri = f"artifacts/{artifact_id}-{filename}"
        self._atomic_bytes(self.run_dir(journal_run.run_id) / storage_uri, data)
        return self._commit_artifact_unlocked(
            journal_path=journal_path,
            journal_run=journal_run,
            artifact_id=artifact_id,
            filename=filename,
            storage_uri=storage_uri,
            role=role,
            producer=producer,
            artifact_type=artifact_type,
            media_type=media_type,
            schema_type=schema_type,
            schema_version_name=schema_version_name,
            content_hash=sha256_bytes(data),
            size_bytes=len(data),
            turn=turn,
            current_agent=current_agent,
        )

    def _commit_artifact_unlocked(
        self,
        *,
        journal_path: Path,
        journal_run: Run,
        artifact_id: str,
        filename: str,
        storage_uri: str,
        role: str,
        producer: str,
        artifact_type: ArtifactType,
        media_type: str,
        schema_type: Optional[str],
        schema_version_name: Optional[str],
        content_hash: str,
        size_bytes: int,
        turn: Optional[int],
        current_agent: Optional[str],
    ) -> Artifact:
        timestamp = utc_now()
        event_id = new_id("evt")
        event_turn = journal_run.current_turn if turn is None else turn
        if event_turn < journal_run.current_turn:
            raise ControlError(
                "EVENT_TURN_REGRESSION",
                "artifact event turn cannot move backward",
                exit_code=ExitCode.integrity,
                details={
                    "run_id": journal_run.run_id,
                    "current_turn": journal_run.current_turn,
                    "event_turn": event_turn,
                },
            )
        event = Event(
            event_id=event_id,
            experiment_id=journal_run.experiment_id,
            run_id=journal_run.run_id,
            sequence=journal_run.event_sequence + 1,
            occurred_at=timestamp,
            event_type=EventType.artifact_created,
            turn=event_turn,
            actor=producer,
            payload=ArtifactCreatedPayload(artifact_id=artifact_id),
        )
        artifact = Artifact(
            artifact_id=artifact_id,
            experiment_id=journal_run.experiment_id,
            run_id=journal_run.run_id,
            producer_event_id=event_id,
            producer=producer,
            artifact_type=artifact_type,
            role=role,
            filename=filename,
            storage_uri=storage_uri,
            content_hash=content_hash,
            media_type=media_type,
            schema_type=schema_type,
            schema_version_name=schema_version_name,
            size_bytes=size_bytes,
            created_at=timestamp,
            retention=RetentionPolicy.experiment,
            owner=journal_run.owner,
        )
        manifest_path = self.artifact_manifest_path(journal_run.run_id)
        manifest = self.artifact_manifest(journal_run.run_id)
        updated_manifest = ArtifactManifest(
            run_id=journal_run.run_id,
            artifacts=(*manifest.artifacts, artifact),
            updated_at=timestamp,
        )
        write_model(
            manifest_path,
            updated_manifest,
            expected_hash=file_hash(manifest_path),
        )
        updates: dict = {
            "artifact_ids": [*journal_run.artifact_ids, artifact_id],
            "event_sequence": event.sequence,
            "current_turn": event_turn,
            "updated_at": timestamp,
        }
        if current_agent is not None:
            updates["current_agent"] = current_agent
        updated_run = self._validated_run(journal_run, updates)
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

    def scheduler_handle(self, run_id: str) -> Optional[SlurmExecutionHandle]:
        path = self.scheduler_handle_path(run_id)
        return self._read(path, SlurmExecutionHandle) if path.exists() else None

    def scheduler_cancellation(self, run_id: str) -> Optional[SlurmCancellationLedger]:
        path = self.scheduler_cancellation_path(run_id)
        return self._read(path, SlurmCancellationLedger) if path.exists() else None

    def scheduler_submission(self, run_id: str) -> Optional[SlurmSubmissionLedger]:
        path = self.scheduler_submission_path(run_id)
        return self._read(path, SlurmSubmissionLedger) if path.exists() else None

    def _record_scheduler_submission_attempt_unlocked(
        self,
        *,
        run_id: str,
        job_name: str,
        script_hash: str,
    ) -> SlurmSubmissionLedger:
        path = self.scheduler_submission_path(run_id)
        existing = (
            self._read(path, SlurmSubmissionLedger)
            if path.exists()
            else SlurmSubmissionLedger(
                run_id=run_id,
                job_name=job_name,
                script_hash=script_hash,
            )
        )
        if existing.job_name != job_name or existing.script_hash != script_hash:
            raise ControlError(
                "SCHEDULER_SUBMISSION_CONFLICT",
                "submission ledger differs from the frozen Slurm job identity",
                exit_code=ExitCode.integrity,
                details={"run_id": run_id},
            )
        updated = SlurmSubmissionLedger.model_validate_json(
            existing.model_copy(
                update={"attempts": (*existing.attempts, utc_now())}
            ).model_dump_json()
        )
        write_model(
            path,
            updated,
            expected_hash=file_hash(path) if path.exists() else None,
        )
        return updated

    def _record_scheduler_cancellation_unlocked(
        self,
        *,
        run_id: str,
        job_id: str,
        succeeded: bool,
        error_code: Optional[str] = None,
    ) -> SlurmCancellationLedger:
        path = self.scheduler_cancellation_path(run_id)
        existing = (
            self._read(path, SlurmCancellationLedger)
            if path.exists()
            else SlurmCancellationLedger(run_id=run_id, job_id=job_id)
        )
        if existing.job_id != job_id:
            raise ControlError(
                "SCHEDULER_JOB_MISMATCH",
                "cancellation ledger job ID differs from the bound run",
                exit_code=ExitCode.integrity,
                details={"run_id": run_id},
            )
        if any(attempt.succeeded for attempt in existing.attempts):
            return existing
        updated = SlurmCancellationLedger.model_validate_json(
            existing.model_copy(
                update={
                    "attempts": (
                        *existing.attempts,
                        SlurmCancellationAttempt(
                            succeeded=succeeded,
                            error_code=error_code,
                        ),
                    )
                }
            ).model_dump_json()
        )
        write_model(
            path,
            updated,
            expected_hash=file_hash(path) if path.exists() else None,
        )
        return updated

    def _bind_scheduler_job_unlocked(
        self, handle: SlurmExecutionHandle
    ) -> tuple[Run, bool]:
        path = self.run_journal_path(handle.run_id)
        journal = read_run_journal(path)
        run = journal.run
        if run.executor != ExecutorKind.slurm or run.partition != "peerd":
            raise ControlError(
                "RUN_NOT_SLURM",
                "scheduler identity can be bound only to a peerd Slurm run",
                exit_code=ExitCode.conflict,
                details={"run_id": handle.run_id, "executor": run.executor.value},
            )
        if run.state not in {RunState.queued, RunState.cancelling}:
            raise ControlError(
                "RUN_NOT_QUEUED",
                "a Slurm job can be bound only while the run is queued or cancelling",
                exit_code=ExitCode.conflict,
                details={"run_id": handle.run_id, "state": run.state.value},
            )
        handle_path = self.scheduler_handle_path(run.run_id)
        if handle_path.exists():
            existing = self._read(handle_path, SlurmExecutionHandle)
            if existing != handle:
                raise ControlError(
                    "SCHEDULER_HANDLE_CONFLICT",
                    "a different scheduler handle already exists for the run",
                    exit_code=ExitCode.integrity,
                    details={"run_id": run.run_id},
                )
        else:
            # Persist scheduler identity before modifying the run journal. If the
            # process stops between these writes, launch can finish the binding
            # from this held-job handle without submitting a duplicate job.
            write_model(handle_path, handle)
        if run.scheduler_job_id is not None:
            if run.scheduler_job_id == handle.job_id:
                return run, False
            raise ControlError(
                "SCHEDULER_JOB_CONFLICT",
                "the run is already bound to a different Slurm job",
                exit_code=ExitCode.conflict,
                details={
                    "run_id": handle.run_id,
                    "existing_job_id": run.scheduler_job_id,
                    "submitted_job_id": handle.job_id,
                },
            )
        timestamp = utc_now()
        event = Event(
            experiment_id=run.experiment_id,
            run_id=run.run_id,
            sequence=run.event_sequence + 1,
            occurred_at=timestamp,
            event_type=EventType.heartbeat,
            turn=run.current_turn,
            stage="scheduler_submission",
            actor="slurm-executor",
            payload=HeartbeatPayload(
                message=(
                    f"Slurm job {handle.job_id} bound on partition peerd while held"
                )
            ),
        )
        updated = self._validated_run(
            run,
            {
                "scheduler_job_id": handle.job_id,
                "event_sequence": event.sequence,
                "updated_at": timestamp,
            },
        )
        commit_run_scheduler_binding(
            path,
            updated,
            event,
            expected_hash=file_hash(path),
        )
        return updated, True

    def bind_scheduler_job(
        self, handle: SlurmExecutionHandle
    ) -> tuple[Run, bool]:
        with self.mutation_lock():
            return self._bind_scheduler_job_unlocked(handle)

    def write_scheduler_script(self, run_id: str, script: str) -> tuple[Path, str]:
        run = self.run(run_id)
        if run.executor != ExecutorKind.slurm:
            raise ControlError(
                "RUN_NOT_SLURM",
                "scheduler scripts belong only to Slurm runs",
                exit_code=ExitCode.conflict,
                details={"run_id": run_id},
            )
        data = script.encode("utf-8")
        content_hash = sha256_bytes(data)
        path = self.scheduler_script_path(run_id)
        with self.mutation_lock():
            if path.exists():
                if file_hash(path) != content_hash:
                    raise ControlError(
                        "SCHEDULER_SCRIPT_CONFLICT",
                        "the frozen Slurm script changed after it was written",
                        exit_code=ExitCode.integrity,
                        details={"run_id": run_id},
                    )
            else:
                self._atomic_bytes(path, data)
                path.chmod(0o700)
        return path, content_hash

    def _mark_scheduler_released_unlocked(
        self, run_id: str
    ) -> SlurmExecutionHandle:
        path = self.scheduler_handle_path(run_id)
        handle = self._read(path, SlurmExecutionHandle)
        if handle.released_at is not None:
            return handle
        updated = SlurmExecutionHandle.model_validate_json(
            handle.model_copy(update={"released_at": utc_now()}).model_dump_json()
        )
        write_model(path, updated, expected_hash=file_hash(path))
        return updated

    def mark_scheduler_released(self, run_id: str) -> SlurmExecutionHandle:
        with self.mutation_lock():
            return self._mark_scheduler_released_unlocked(run_id)

    def scheduler_accounting(self, run_id: str) -> Optional[SlurmAccounting]:
        path = self.scheduler_accounting_path(run_id)
        if not path.exists():
            return None
        accounting = self._read(path, SlurmAccounting)
        raw_path = self.scheduler_accounting_raw_path(run_id)
        if (
            accounting.raw_output_path != raw_path.name
            or not raw_path.is_file()
            or raw_path.is_symlink()
            or file_hash(raw_path) != accounting.raw_output_hash
        ):
            raise ControlError(
                "SCHEDULER_ACCOUNTING_TAMPERED",
                "durable Slurm accounting raw output is missing or does not match its hash",
                exit_code=ExitCode.integrity,
                details={"run_id": run_id},
            )
        return accounting

    def _record_system_artifact_unlocked(
        self,
        *,
        run_id: str,
        filename: str,
        role: str,
        data: bytes,
        artifact_type: ArtifactType,
        media_type: str,
        schema_type: Optional[str] = None,
        schema_version_name: Optional[str] = None,
    ) -> tuple[Artifact, bool]:
        manifest = self.artifact_manifest(run_id)
        existing = next(
            (artifact for artifact in manifest.artifacts if artifact.role == role),
            None,
        )
        content_hash = sha256_bytes(data)
        if existing is not None:
            if (
                existing.filename != filename
                or existing.content_hash != content_hash
                or existing.size_bytes != len(data)
            ):
                raise ControlError(
                    "SYSTEM_ARTIFACT_CONFLICT",
                    "a scheduler artifact role already refers to different content",
                    exit_code=ExitCode.integrity,
                    details={"run_id": run_id, "role": role},
                )
            try:
                verify_artifact(
                    self.artifact_path(existing),
                    existing.content_hash,
                    existing.size_bytes,
                    root=self.run_dir(run_id),
                )
            except IntegrityError as exc:
                raise ControlError(
                    "SYSTEM_ARTIFACT_TAMPERED",
                    "a durable scheduler artifact failed content verification",
                    exit_code=ExitCode.integrity,
                    details={"run_id": run_id, "role": role},
                ) from exc
            journal_path = self.run_journal_path(run_id)
            journal = read_run_journal(journal_path)
            if existing.artifact_id not in journal.run.artifact_ids:
                # The manifest is written before the journal event by the shared
                # artifact path. Recover a stop at that boundary deterministically
                # from the immutable artifact descriptor rather than duplicating it.
                timestamp = utc_now()
                event = Event(
                    event_id=existing.producer_event_id,
                    experiment_id=journal.run.experiment_id,
                    run_id=run_id,
                    sequence=journal.run.event_sequence + 1,
                    occurred_at=timestamp,
                    event_type=EventType.artifact_created,
                    turn=journal.run.current_turn,
                    actor=existing.producer,
                    payload=ArtifactCreatedPayload(
                        artifact_id=existing.artifact_id
                    ),
                )
                updated = self._validated_run(
                    journal.run,
                    {
                        "artifact_ids": [
                            *journal.run.artifact_ids,
                            existing.artifact_id,
                        ],
                        "event_sequence": event.sequence,
                        "updated_at": timestamp,
                    },
                )
                commit_run_event(
                    journal_path,
                    updated,
                    event,
                    expected_hash=file_hash(journal_path),
                )
            return existing, False
        journal_path = self.run_journal_path(run_id)
        journal = read_run_journal(journal_path)
        artifact = self._record_artifact_bytes_unlocked(
            journal_path=journal_path,
            journal_run=journal.run,
            filename=filename,
            role=role,
            data=data,
            producer="slurm-reconciler",
            artifact_type=artifact_type,
            media_type=media_type,
            schema_type=schema_type,
            schema_version_name=schema_version_name,
            turn=None,
            current_agent=None,
        )
        return artifact, True

    def ensure_scheduler_artifacts(self, run_id: str) -> tuple[Artifact, ...]:
        """Expose immutable terminal Slurm evidence through the shared artifact API."""

        with self.mutation_lock():
            accounting = self.scheduler_accounting(run_id)
            if accounting is None:
                raise ControlError(
                    "SCHEDULER_ACCOUNTING_UNAVAILABLE",
                    "terminal Slurm accounting has not been persisted",
                    exit_code=ExitCode.not_found,
                    details={"run_id": run_id},
                )
            raw_path = self.scheduler_accounting_raw_path(run_id)
            artifacts = [
                self._record_system_artifact_unlocked(
                    run_id=run_id,
                    filename="scheduler-accounting.json",
                    role="slurm_accounting",
                    data=canonical_json_bytes(accounting),
                    artifact_type=ArtifactType.manifest,
                    media_type="application/json",
                    schema_type="caribou.slurm_accounting",
                    schema_version_name="v1",
                )[0],
                self._record_system_artifact_unlocked(
                    run_id=run_id,
                    filename="scheduler-accounting.raw",
                    role="slurm_accounting_raw",
                    data=raw_path.read_bytes(),
                    artifact_type=ArtifactType.log,
                    media_type="text/plain",
                )[0],
            ]
            handle = self.scheduler_handle(run_id)
            if handle is not None:
                script_path = self.run_dir(run_id) / handle.script_path
                if (
                    Path(handle.script_path).name != handle.script_path
                    or not script_path.is_file()
                    or script_path.is_symlink()
                    or file_hash(script_path) != handle.script_hash
                ):
                    raise ControlError(
                        "SCHEDULER_SCRIPT_TAMPERED",
                        "the submitted Slurm script is missing or differs from its durable hash",
                        exit_code=ExitCode.integrity,
                        details={"run_id": run_id},
                    )
                artifacts.append(
                    self._record_system_artifact_unlocked(
                        run_id=run_id,
                        filename="slurm-job.sh",
                        role="slurm_job_script",
                        data=script_path.read_bytes(),
                        artifact_type=ArtifactType.code,
                        media_type="text/x-shellscript",
                    )[0]
                )
                stdout_name = handle.stdout_path.replace("%j", handle.job_id)
                if Path(stdout_name).name != stdout_name:
                    raise ControlError(
                        "SCHEDULER_STDOUT_PATH_INVALID",
                        "the durable Slurm stdout path is not a safe run-local filename",
                        exit_code=ExitCode.integrity,
                        details={"run_id": run_id},
                    )
                stdout_path = self.run_dir(run_id) / stdout_name
                if stdout_path.is_file() and not stdout_path.is_symlink():
                    artifacts.append(
                        self._record_system_artifact_unlocked(
                            run_id=run_id,
                            filename="slurm-stdout.log",
                            role="slurm_stdout",
                            data=stdout_path.read_bytes(),
                            artifact_type=ArtifactType.log,
                            media_type="text/plain",
                        )[0]
                    )
            return tuple(artifacts)

    def write_scheduler_accounting(
        self,
        accounting: SlurmAccounting,
        *,
        raw_output: str,
    ) -> tuple[SlurmAccounting, bool]:
        with self.mutation_lock():
            run = self.run(accounting.run_id)
            if run.executor != ExecutorKind.slurm:
                raise ControlError(
                    "RUN_NOT_SLURM",
                    "scheduler accounting belongs only to Slurm runs",
                    exit_code=ExitCode.conflict,
                    details={"run_id": accounting.run_id},
                )
            if run.scheduler_job_id != accounting.job_id:
                raise ControlError(
                    "SCHEDULER_JOB_MISMATCH",
                    "scheduler accounting job ID differs from the durable run",
                    exit_code=ExitCode.integrity,
                    details={
                        "run_id": accounting.run_id,
                        "run_job_id": run.scheduler_job_id,
                        "accounting_job_id": accounting.job_id,
                    },
                )
            raw = raw_output.encode("utf-8")
            if sha256_bytes(raw) != accounting.raw_output_hash:
                raise ControlError(
                    "SCHEDULER_ACCOUNTING_HASH_MISMATCH",
                    "raw scheduler accounting does not match its declared hash",
                    exit_code=ExitCode.integrity,
                )
            path = self.scheduler_accounting_path(accounting.run_id)
            if path.exists():
                existing = self._read(path, SlurmAccounting)
                if existing.raw_output_hash == accounting.raw_output_hash:
                    return existing, False
                raise ControlError(
                    "SCHEDULER_ACCOUNTING_CONFLICT",
                    "terminal scheduler accounting changed after collection",
                    exit_code=ExitCode.integrity,
                    details={"run_id": accounting.run_id},
                )
            raw_path = self.scheduler_accounting_raw_path(accounting.run_id)
            self._atomic_bytes(raw_path, raw)
            write_model(path, accounting)
            return accounting, True

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
