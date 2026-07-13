---
name: validate-caribou-system
description: Design and execute CARIBOU systems validation for CLI and web parity, experiment lifecycle contracts, containers, Slurm execution, checkpoints, restart and cancellation, concurrency, authentication and ownership boundaries, resource telemetry, scaling, portability, and fault recovery. Use when a CARIBOU platform capability must move from implemented to operationally or independently validated.
---

# Validate CARIBOU System

Test the exact claim under the actual boundary it names. Do not use component tests
as substitutes for scheduler, recovery, scaling, security, or multi-user evidence.

## Define the validation contract

1. Identify the claim, implementation version, environment, threat/failure model,
   and acceptance threshold.
2. Resolve `CARIBOU_PROGRAM_HOME` and read its `policy.yaml` before starting real
   services, containers, model calls, fault injection, or scheduler jobs. Never
   infer authority from the public template.
3. Inspect current code and tests; do not rely on an older architecture map alone.
4. Read [references/validation-matrix.md](references/validation-matrix.md) and select
   the smallest set of layers that directly tests the claim.
5. Freeze inputs, commands, resource limits, telemetry, failure taxonomy, artifact
   locations, and cleanup/reconciliation procedure.

Label mocked, simulated, local-real, scheduler-real, and independent-reproduction
evidence distinctly.

For scheduler-real evidence, verify that the frozen spec, rendered script,
scheduler record, and run provenance all resolve to partition `peerd`. Treat any
other partition or an overridable partition as a failed authorization/control
test.

## Build a layered test

Use layers in increasing cost/risk:

1. deterministic unit/state-machine tests;
2. serialization/schema/migration contract tests;
3. external-process CLI tests;
4. API/WebSocket and browser tests;
5. real container/model/storage integration;
6. real scheduler and service deployment;
7. controlled perturbation, concurrency, scaling, and portability;
8. isolated independent replay.

Stop early when a lower layer reveals a defect that invalidates higher-cost work.
Repair under `develop-caribou-platform`, rerun the failed layer, then continue.

## Validate equivalence

For CLI/web/benchmark equivalence, resolve one frozen experiment spec through each
interface and compare:

- exact model, prompt, blueprint, RAG/tools, memory, turns, budget, sandbox, and
  evaluator;
- agent topology, messages/delegations, executed code, dataset state transitions,
  metrics, failures, artifacts, and provenance; and
- cancellation, reconnect, restart, and terminal-state semantics.

Presentation may differ. Scientific inputs and interpretation must not.

## Validate long-running behavior

Inject one declared failure at a time. Cover client disconnect, service restart,
duplicate request/event, model timeout, sandbox crash, scheduler rejection,
preemption/time limit, out-of-memory, full scratch, partial artifact write, and
checkpoint incompatibility when relevant.

- Confirm the failure is detected and typed.
- Confirm durable state matches the real backend.
- Confirm retry/resume respects idempotency and budget.
- Confirm failed attempts remain preserved.
- Compare resumed output to a clean run within declared tolerances.

Never perform destructive or availability-impacting fault injection outside the
explicit policy envelope.

## Validate security boundaries

Define assets, actors, trust boundaries, and abuse cases. Test server-side
authentication, authorization, ownership, path traversal, artifact isolation,
secret redaction, allowed origins, WebSocket access, model/data egress, container
mount/network/process limits, scheduler identity, and audit records as applicable.

Absence of an observed exploit is not a security proof. Report the tested boundary,
uncovered threats, and residual risk; use “scoped execution” unless stronger claims
are directly supported.

## Validate scaling

Predeclare the independent variable and distinguish queue, startup, orchestration,
model inference, biological computation, checkpointing, and artifact time. Record
CPU/GPU/memory/storage/network, throughput, utilization, saturation, cost, and
failure rate. Dataset-size accuracy alone is not HPC scaling.

## Preserve and judge evidence

Store the versioned validation plan, commands, raw events/logs, telemetry, backend
IDs, outputs, hashes, failures, cleanup result, and environment manifest. Require an
independent reviewer before `Validated`; otherwise use `Evidence collected`.

Report pass/fail per acceptance criterion, deviations, residual risks, exact claim
supported, and narrower wording when the claim fails.
