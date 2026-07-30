# ADR 0001: Shared experiment and run domain contract

- Status: Accepted
- Date: 2026-07-13
- Owners: CARIBOU platform and evidence program

## Context

The command-line runner, web service, frontend, and benchmark tooling evolved
with related but incompatible representations of sessions, attempts, events,
artifacts, metrics, failures, checkpoints, and budgets. Permissive records and
implicit lifecycle behavior make it possible for interfaces to accept fields
that execution ignores, for provenance to be lost, and for a failed attempt to
be silently rewritten as though it later succeeded.

CARIBOU needs one versioned contract that long-running agents and human-facing
interfaces can validate before consuming compute. It must preserve every
attempt, support independent evidence review, and remain useful when the CLI,
web service, scheduler adapter, and benchmark runner are developed separately.

## Decision

The canonical Python models live in `caribou.domain`. They are not owned by the
web server, frontend, execution runner, or benchmark package. JSON Schemas under
`schemas/domain/v1` are generated from these models for non-Python consumers.

The v1 contract includes `ExperimentSpec`, `Experiment`, `Run`, `Event`,
`Artifact`, `FailureRecord`, `MetricRecord`, `Checkpoint`, `BudgetRecord`, and
`Aggregate`. Records are immutable, recursively reject unknown fields, require
an exact schema version, use typed prefixed identifiers, require UTC timestamps,
and identify content with SHA-256 hashes. Slurm records reject any partition
other than `peerd`, reflecting the current program authority.

`Run` represents one attempt, not a mutable logical job. Succeeded, failed,
cancelled, rejected, and resumable attempts are terminal. A failed attempt is
never reopened. An interruption can be classified as `resumable` only when a
checkpoint is attached; resumption creates a new attempt linked by
`resumed_from_run_id` and `resume_checkpoint_id`. This preserves both the
interrupted attempt and the later outcome.

Lifecycle transitions are explicit. A repeated request for the current state
is idempotent and creates no duplicate event. State snapshots and corresponding
events can be committed as one atomically replaced `RunJournal`, guarded by a
cross-process lock and compare-and-swap hash. Standalone record writes also use
same-directory temporary files, `fsync`, and atomic replacement.

Host-side development and control-plane tests use one shared Conda prefix.
Repository-local virtualenvs and per-run Conda environments are not supported.
Biological dependencies remain in the versioned Docker/Apptainer image so the
host environment is not misreported as the scientific execution environment.

## Compatibility policy

| Source record | v1 behavior |
|---|---|
| Canonical v1 domain record | Strictly validate and load |
| Unversioned web session/event | Preserve source hash; extract observations; require provenance enrichment |
| Unversioned artifact/TODO ledger | Preserve source hash; extract safe identity fields; require enrichment |
| Unversioned benchmark ledger | Preserve source hash; do not promote historical scores to validated metrics |
| Unknown or future version | Quarantine with an explicit unsupported-version reason |
| Corrupt or non-object record | Quarantine; never skip silently |

The migration registry produces deterministic `MigrationReport` records. It does
not turn an old session into a canonical run because the old format lacks frozen
specification, code, container, prompt, model, input, and resource provenance.
Running the inspection twice produces the same migration identity and report.
Original bytes remain authoritative and are identified by their content hash.

An incomplete JSONL tail is an integrity failure, not an invitation to truncate
history automatically. Atomic readers ignore abandoned temporary files. If a
crash happens before replacement, the prior record remains; if it happens after
replacement but before directory synchronization completes, the new bytes still
have to parse as one complete record and the operation reports a persistence
error for reconciliation.

## Consequences

- CLI, web, and benchmark entry points must become adapters to this domain.
- Existing sessions and ledgers need explicit migration code; loaders must not
  invent missing provenance or silently skip corrupt records.
- Token streaming remains ephemeral. Durable scientific and operational events
  are append-only and cannot be trimmed from the evidence ledger.
- A logical analysis resumed twice will have multiple immutable `Run` records.
  Aggregation must predeclare which attempts are included and explain every
  exclusion.
- Changes incompatible with v1 require a new schema version and migration, not
  relaxation of v1 validation.

## Rejected alternatives

- Keeping server models canonical would couple CLI and benchmark execution to a
  transport layer and preserve frontend/backend drift.
- Reopening `failed` as `queued` would erase the boundary between attempts and
  make failure rates and recovery evidence unreliable.
- Updating a snapshot and JSONL event file independently would permit crashes to
  expose only half of a transition. The atomic journal is the authoritative
  state/event persistence unit.
