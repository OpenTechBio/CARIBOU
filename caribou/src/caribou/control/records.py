"""Versioned operational records kept outside the scientific domain objects."""

from __future__ import annotations

from typing import Dict, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

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
