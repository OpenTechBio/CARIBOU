# CARIBOU systems-validation matrix

| Claim/surface | Required validation |
|---|---|
| machine CLI | external-process tests for stdout/stderr, JSON/JSONL schemas, exit codes, no prompts, idempotency |
| durable lifecycle | restart/reconcile tests with stable IDs, atomic state, duplicate delivery, terminal outcomes |
| CLI/web equivalence | same frozen spec and compared resolved inputs, events, code, state, metrics, artifacts |
| Slurm operation | real job IDs/states, resources, logs, cancellation, rejection, completion, service restart reconciliation |
| checkpoint/resume | controlled interruption at each declared boundary, compatibility checks, output comparison |
| local inference | exact model artifact and server/hardware telemetry with verified absence of external model calls |
| container isolation | mounts, network, subprocess, secrets, filesystem writes, cleanup, runtime identity |
| multi-user UI | authentication, authorization, session/job ownership, paths, artifacts, concurrent access, audit log |
| browser resilience | disconnect/reconnect, stale cursor, service restart, cancellation, artifact retrieval |
| scaling | frozen size/concurrency sweep with queue/runtime/resource/throughput/cost/failure telemetry |
| portability | same spec across declared environments with output/provenance comparison |
| reproducibility | isolated clean replay from immutable release and declared tolerances |

## Acceptance-record fields

- claim and maturity level sought
- code/container/config identity
- environment and policy record
- predeclared cases and thresholds
- command/spec and run IDs
- raw evidence paths and hashes
- observed result and deviations
- reviewer ID and independence statement
- decision: pass, fail, partial, blocked, or not claimed
- exact wording supported

## Fault-test safety

- Use disposable data, services, namespaces, and job IDs.
- Bound time, retries, resources, and concurrency.
- Verify cleanup without erasing failure records.
- Do not probe infrastructure or other users outside authorization.
- Abort on unexpected data/credential exposure or uncontrolled impact.
