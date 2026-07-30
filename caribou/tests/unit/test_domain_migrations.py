"""Legacy records are preserved and inspected without invented provenance."""

from __future__ import annotations

import json
from pathlib import Path

from caribou.domain.migrations import inspect_legacy_record, legacy_hash


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_unversioned_session_migration_plan_is_deterministic_and_noninventive() -> None:
    source = load("web_session.json")
    first = inspect_legacy_record(
        source,
        source_kind="web_session",
        source_uri="legacy://sessions/session-2025-01/session.json",
    )
    second = inspect_legacy_record(
        source,
        source_kind="web_session",
        source_uri="legacy://sessions/session-2025-01/session.json",
    )
    assert first == second
    assert first.disposition == "requires_enrichment"
    assert first.preserved_source_hash == legacy_hash(source)
    assert first.extracted["id"] == "session-2025-01"
    assert "frozen experiment specification hash" in first.missing_provenance
    assert "spec_hash" not in first.extracted


def test_benchmark_ledger_is_not_promoted_to_validated_metric() -> None:
    report = inspect_legacy_record(
        load("benchmark_ledger.json"),
        source_kind="benchmark_ledger",
        source_uri="legacy://benchmarks/metadata.jsonl#1",
    )
    assert report.disposition == "requires_enrichment"
    assert report.target_schema_version == "caribou.metric.v1"
    assert "score" not in report.extracted


def test_unsupported_future_and_corrupt_records_are_quarantined() -> None:
    future = inspect_legacy_record(
        load("unsupported_future_session.json"),
        source_kind="web_session",
        source_uri="legacy://sessions/future/session.json",
    )
    assert future.disposition == "quarantined"
    assert "unsupported versioned source" in future.quarantine_reason
    run_v2 = inspect_legacy_record(
        {"schema_version": "caribou.run.v2", "id": "future-run"},
        source_kind="web_session",
        source_uri="legacy://sessions/future/run-v2.json",
    )
    assert run_v2.disposition == "quarantined"
    corrupt = inspect_legacy_record(
        ["not", "an", "object"],
        source_kind="artifact",
        source_uri="legacy://artifacts/corrupt.json",
    )
    assert corrupt.disposition == "quarantined"
