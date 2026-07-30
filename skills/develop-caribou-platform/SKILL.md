---
name: develop-caribou-platform
description: Design, implement, review, and test CARIBOU platform changes across the CLI, FastAPI server, Angular web UI, execution runners, model providers, sandboxes, sessions, provenance, scheduler integration, checkpoints, and experiment control plane. Use when changing CARIBOU architecture or behavior and when CLI/web/benchmark parity, long-running-agent operability, recovery, security boundaries, or shared domain models are relevant.
---

# Develop CARIBOU Platform

Improve CARIBOU as one research execution system rather than a collection of
interface-specific behaviors.

## Map the change

1. Locate the repository root and inspect `git status` before editing.
2. Resolve `CARIBOU_PROGRAM_HOME`, read `state.json`, and load the program goal
   and repository map through its `documents` references. Never assume manuscript
   material is stored in the public checkout.
3. Inspect every affected path: CLI, `execution`, `server`, frontend, blueprints,
   providers, sandbox, benchmark drivers, tests, documentation, and release schema.
4. Read [references/architecture-guardrails.md](references/architecture-guardrails.md)
   for current hotspots and target invariants.
5. State whether the request is diagnosis/review or authorized implementation.
   Do not turn a read-only review into a code change.

## Choose the owning layer

Put scientific and lifecycle semantics into shared domain/services code. Keep CLI,
FastAPI/WebSocket, Angular, and benchmark launchers as adapters.

Before adding behavior to an interface, ask:

- Is this a run-specification, lifecycle, model-resolution, memory, event, metric,
  artifact, failure, budget, or checkpoint concern?
- Does another interface already implement a parallel version?
- Can the shared concept be extracted without changing current behavior first?
- What compatibility/migration path is needed for persisted sessions and outputs?

Do not create a third runner or another unversioned result representation to ship a
feature quickly.

## Preserve the automation contract

Agent-facing behavior must be non-interactive and machine-readable:

- emit one documented JSON object or versioned JSON Lines on stdout;
- send human diagnostics to stderr;
- use stable exit codes and object IDs;
- never prompt in machine mode;
- support idempotent mutation and cheap side-effect-free status reads;
- expose capability and schema discovery;
- record version, commit, timestamps, state, and next operations;
- use atomic writes and explicit state transitions; and
- avoid secrets in arguments, logs, events, and artifacts.

Do not document proposed commands as implemented until command discovery and tests
prove they exist.

## Implement safely

1. Add or change the shared model/service first.
2. Migrate CLI and web adapters to it in the same change when their semantics are
   affected.
3. Preserve existing user workflows or document an intentional migration.
4. Validate paths, permissions, ownership, budgets, and resource requests at the
   boundary and again before execution.
5. Make failures typed and durable. Preserve attempts rather than overwriting them
   on retry.
6. Define cancellation, restart, resume, timeout, and partial-write behavior.
7. Separate display/event replay from computation checkpoint recovery.
8. Capture provenance automatically from actual resolved values, not merely user
   input.

For Slurm implementation, compose with `create-traceable-slurm` for locked scripts
and `submit-sbatch-dag` for dependency-safe workflows. Keep scheduler adapters
behind a portable execution interface. CARIBOU's live policy requires partition
`peerd`: default it in shared run resolution, render it into submitted scripts and
provenance, and reject conflicting interface or environment overrides.

Use one shared Conda prefix for host-side CLI, web-control-plane, build, and test
work. Read the live program decisions for its site-specific location. Never create
a repository-local virtualenv or a Conda environment per checkout, experiment, or
Slurm job. Disable Python user-site loading for tests and evidence capture so the
recorded prefix cannot silently import packages from `~/.local`. Keep biological
execution dependencies in the versioned
Docker/Apptainer image, and record the Conda prefix identity for host-side evidence
without treating it as the analysis environment.

## Test across boundaries

Use proportionate layers:

1. model/state-transition unit tests;
2. adapter/serialization and migration tests;
3. external-process CLI contract tests;
4. backend API/WebSocket tests;
5. frontend build plus browser tests when UI behavior changes;
6. CLI/web golden-run comparison for scientific semantics;
7. real container/scheduler/provider smoke tests only with authorization; and
8. restart, duplicate delivery, cancellation, timeout, and partial-failure tests for
   long-running work.

Test negative paths and assert persisted records, not only terminal console text.
Do not weaken or delete unrelated tests to accommodate a change.

## Maintain evidence and claims

- Distinguish implementation, pilot operation, production validation, security
  validation, and independent reproduction.
- Update architecture and operator documentation with the exact boundary changed.
- Record schema and compatibility changes.
- Do not convert passing mocked tests into claims of HPC scalability, security,
  biological validity, or recovery.
- If a change affects manuscript claims or experiments, identify which evidence
  must be regenerated; never silently reuse incompatible results.

## Deliver the change

Report:

1. outcome and owning architectural layer;
2. files and interfaces changed;
3. compatibility and migration effects;
4. validation performed and intentionally untested real-system surfaces;
5. security, provenance, budget, and scientific-semantics impact; and
6. remaining blockers or follow-on experiment requirements.
