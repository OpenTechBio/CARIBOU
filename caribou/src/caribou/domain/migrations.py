"""Fail-closed registry for inspecting legacy CARIBOU persisted records.

Legacy records lack enough immutable provenance to become canonical Runs.  This
module therefore produces deterministic migration reports: usable fields are
extracted, missing provenance remains explicit, and corrupt/future records are
quarantined without altering or replacing the original bytes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Literal, Optional

from pydantic import Field, JsonValue, StrictStr

from .models import ContentHash, DomainModel, NonEmptyStr


class MigrationReport(DomainModel):
    schema_version: Literal["caribou.migration_report.v1"] = (
        "caribou.migration_report.v1"
    )
    migration_id: StrictStr = Field(pattern=r"^migration_[0-9a-f]{32}$")
    source_kind: Literal[
        "web_session",
        "web_event",
        "artifact",
        "todo_ledger",
        "benchmark_ledger",
    ]
    source_uri: NonEmptyStr
    preserved_source_hash: ContentHash
    detected_schema_version: Optional[NonEmptyStr] = None
    target_schema_version: Optional[NonEmptyStr] = None
    disposition: Literal["requires_enrichment", "quarantined", "already_canonical"]
    extracted: Dict[StrictStr, JsonValue] = Field(default_factory=dict)
    missing_provenance: list[NonEmptyStr] = Field(default_factory=list)
    warnings: list[NonEmptyStr] = Field(default_factory=list)
    quarantine_reason: Optional[NonEmptyStr] = None


LEGACY_MIGRATION_REGISTRY = {
    "web_session": "inspect_legacy_record",
    "web_event": "inspect_legacy_record",
    "artifact": "inspect_legacy_record",
    "todo_ledger": "inspect_legacy_record",
    "benchmark_ledger": "inspect_legacy_record",
}

_CANONICAL_VERSIONS = {
    "web_session": "caribou.run.v1",
    "web_event": "caribou.event.v1",
    "artifact": "caribou.artifact.v1",
    "benchmark_ledger": "caribou.metric.v1",
}


def canonical_legacy_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def legacy_hash(value: JsonValue) -> str:
    return f"sha256:{hashlib.sha256(canonical_legacy_bytes(value)).hexdigest()}"


def inspect_legacy_record(
    value: JsonValue,
    *,
    source_kind: Literal[
        "web_session", "web_event", "artifact", "todo_ledger", "benchmark_ledger"
    ],
    source_uri: NonEmptyStr,
) -> MigrationReport:
    """Create a deterministic plan without inventing absent provenance."""

    source_hash = legacy_hash(value)
    migration_id = f"migration_{source_hash.split(':', 1)[1][:32]}"
    if not isinstance(value, dict):
        return MigrationReport(
            migration_id=migration_id,
            source_kind=source_kind,
            source_uri=source_uri,
            preserved_source_hash=source_hash,
            disposition="quarantined",
            quarantine_reason="legacy record is not a JSON object",
        )

    detected = value.get("schema_version")
    if detected is not None and not isinstance(detected, str):
        return MigrationReport(
            migration_id=migration_id,
            source_kind=source_kind,
            source_uri=source_uri,
            preserved_source_hash=source_hash,
            disposition="quarantined",
            quarantine_reason="schema_version is not a string",
        )
    canonical_version = _CANONICAL_VERSIONS.get(source_kind)
    if detected and canonical_version and detected == canonical_version:
        return MigrationReport(
            migration_id=migration_id,
            source_kind=source_kind,
            source_uri=source_uri,
            preserved_source_hash=source_hash,
            detected_schema_version=detected,
            target_schema_version=detected,
            disposition="already_canonical",
        )
    if detected is not None:
        return MigrationReport(
            migration_id=migration_id,
            source_kind=source_kind,
            source_uri=source_uri,
            preserved_source_hash=source_hash,
            detected_schema_version=detected,
            disposition="quarantined",
            quarantine_reason="unsupported versioned source; no registered migration",
        )

    identifier_keys = {
        "web_session": ("session_id", "id"),
        "web_event": ("event_id", "id"),
        "artifact": ("artifact_id", "id", "filename"),
        "todo_ledger": ("session_id", "run_id"),
        "benchmark_ledger": ("run_id", "benchmark_id", "id"),
    }[source_kind]
    found_identifier = next(
        (value[key] for key in identifier_keys if value.get(key)), None
    )
    if found_identifier is None or not isinstance(found_identifier, str):
        return MigrationReport(
            migration_id=migration_id,
            source_kind=source_kind,
            source_uri=source_uri,
            preserved_source_hash=source_hash,
            disposition="quarantined",
            quarantine_reason="legacy record has no usable stable identifier",
        )

    extractable_keys = (
        "id",
        "session_id",
        "run_id",
        "benchmark_id",
        "event_id",
        "status",
        "created_at",
        "updated_at",
        "model",
        "provider",
        "filename",
        "artifact_type",
    )
    extracted = {key: value[key] for key in extractable_keys if key in value}
    return MigrationReport(
        migration_id=migration_id,
        source_kind=source_kind,
        source_uri=source_uri,
        preserved_source_hash=source_hash,
        target_schema_version={
            "web_session": "caribou.run.v1",
            "web_event": "caribou.event.v1",
            "artifact": "caribou.artifact.v1",
            "todo_ledger": "caribou.artifact.v1",
            "benchmark_ledger": "caribou.metric.v1",
        }[source_kind],
        disposition="requires_enrichment",
        extracted=extracted,
        missing_provenance=[
            "canonical experiment identity",
            "frozen experiment specification hash",
            "immutable code and container identity",
            "complete model, prompt, blueprint, input, and resource resolution",
        ],
        warnings=[
            "legacy status and timestamps are observations, not validated lifecycle evidence"
        ],
    )
