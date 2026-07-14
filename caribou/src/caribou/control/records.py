"""Versioned operational records kept outside the scientific domain objects."""

from __future__ import annotations

from typing import Dict, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from caribou.domain.models import (
    Artifact,
    ContentHash,
    ExperimentId,
    NonEmptyStr,
    RunId,
    UtcDatetime,
    utc_now,
)


class ControlRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
    )


class IdempotencyClaim(ControlRecord):
    spec_hash: ContentHash
    experiment_id: ExperimentId
    run_ids: Tuple[RunId, ...]
    created_at: UtcDatetime = Field(default_factory=utc_now)


class StoreIndex(ControlRecord):
    schema_version: Literal["caribou.control_index.v1"] = "caribou.control_index.v1"
    experiments: Dict[ExperimentId, Tuple[RunId, ...]] = Field(default_factory=dict)
    runs: Dict[RunId, ExperimentId] = Field(default_factory=dict)
    idempotency: Dict[ContentHash, IdempotencyClaim] = Field(default_factory=dict)
    updated_at: UtcDatetime = Field(default_factory=utc_now)


class ExecutionHandle(ControlRecord):
    schema_version: Literal["caribou.execution_handle.v1"] = (
        "caribou.execution_handle.v1"
    )
    run_id: RunId
    pid: StrictInt = Field(gt=0)
    hostname: NonEmptyStr
    process_start_identity: NonEmptyStr
    launch_nonce: NonEmptyStr
    worker_module: NonEmptyStr
    log_path: NonEmptyStr
    launched_at: UtcDatetime = Field(default_factory=utc_now)


class SlurmExecutionHandle(ControlRecord):
    schema_version: Literal["caribou.slurm_execution_handle.v1"] = (
        "caribou.slurm_execution_handle.v1"
    )
    run_id: RunId
    job_id: NonEmptyStr
    partition: Literal["peerd"] = "peerd"
    account: Optional[NonEmptyStr] = None
    qos: Optional[NonEmptyStr] = None
    script_path: NonEmptyStr
    script_hash: ContentHash
    stdout_path: NonEmptyStr
    submitted_at: UtcDatetime = Field(default_factory=utc_now)
    released_at: Optional[UtcDatetime] = None

    @model_validator(mode="after")
    def validate_root_job_id(self) -> "SlurmExecutionHandle":
        if not self.job_id.isascii() or not self.job_id.isdigit():
            raise ValueError("Slurm job_id must be one numeric root job ID")
        return self


class SlurmSubmissionLedger(ControlRecord):
    schema_version: Literal["caribou.slurm_submission_ledger.v1"] = (
        "caribou.slurm_submission_ledger.v1"
    )
    run_id: RunId
    job_name: NonEmptyStr
    script_hash: ContentHash
    attempts: Tuple[UtcDatetime, ...] = ()

    @model_validator(mode="after")
    def validate_attempt_order(self) -> "SlurmSubmissionLedger":
        if tuple(sorted(self.attempts)) != self.attempts:
            raise ValueError("Slurm submission attempts must be time ordered")
        return self


class SlurmAccounting(ControlRecord):
    schema_version: Literal["caribou.slurm_accounting.v1"] = (
        "caribou.slurm_accounting.v1"
    )
    run_id: RunId
    job_id: NonEmptyStr
    partition: Literal["peerd"] = "peerd"
    state: NonEmptyStr
    terminal: StrictBool
    exit_code: Optional[NonEmptyStr] = None
    elapsed_seconds: StrictInt = Field(ge=0)
    allocated_cpus: StrictInt = Field(ge=0)
    requested_memory: Optional[NonEmptyStr] = None
    max_rss_kib: Optional[StrictInt] = Field(default=None, ge=0)
    node_list: Optional[NonEmptyStr] = None
    started_at_raw: Optional[NonEmptyStr] = None
    ended_at_raw: Optional[NonEmptyStr] = None
    raw_output_path: NonEmptyStr
    raw_output_hash: ContentHash
    consistent_with_run: Optional[StrictBool] = None
    recorded_at: UtcDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_root_job_id(self) -> "SlurmAccounting":
        if not self.job_id.isascii() or not self.job_id.isdigit():
            raise ValueError("Slurm job_id must be one numeric root job ID")
        return self


class SlurmCancellationAttempt(ControlRecord):
    attempted_at: UtcDatetime = Field(default_factory=utc_now)
    succeeded: StrictBool
    error_code: Optional[NonEmptyStr] = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "SlurmCancellationAttempt":
        if self.succeeded == (self.error_code is not None):
            raise ValueError("successful cancellation has no error; failure requires one")
        return self


class SlurmCancellationLedger(ControlRecord):
    schema_version: Literal["caribou.slurm_cancellation_ledger.v1"] = (
        "caribou.slurm_cancellation_ledger.v1"
    )
    run_id: RunId
    job_id: NonEmptyStr
    attempts: Tuple[SlurmCancellationAttempt, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> "SlurmCancellationLedger":
        if not self.job_id.isascii() or not self.job_id.isdigit():
            raise ValueError("Slurm job_id must be one numeric root job ID")
        if sum(attempt.succeeded for attempt in self.attempts) > 1:
            raise ValueError("Slurm cancellation can be durably successful only once")
        return self


class CancelRequest(ControlRecord):
    schema_version: Literal["caribou.cancel_request.v1"] = "caribou.cancel_request.v1"
    run_id: RunId
    requested_at: UtcDatetime = Field(default_factory=utc_now)
    actor: NonEmptyStr
    reason: NonEmptyStr


class ArtifactManifest(ControlRecord):
    schema_version: Literal["caribou.artifact_manifest.v1"] = (
        "caribou.artifact_manifest.v1"
    )
    run_id: RunId
    artifacts: Tuple[Artifact, ...] = ()
    updated_at: UtcDatetime = Field(default_factory=utc_now)

    def artifact(self, artifact_id: str) -> Optional[Artifact]:
        return next(
            (item for item in self.artifacts if item.artifact_id == artifact_id),
            None,
        )

    @model_validator(mode="after")
    def validate_unique_artifacts(self) -> "ArtifactManifest":
        identifiers = [artifact.artifact_id for artifact in self.artifacts]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("artifact manifest IDs must be unique")
        return self
