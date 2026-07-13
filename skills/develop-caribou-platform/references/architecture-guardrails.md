# CARIBOU architecture guardrails

## Current implementation map

| Surface | Current owner |
|---|---|
| CLI commands | `caribou/src/caribou/cli/` |
| primary CLI orchestration | `caribou/src/caribou/execution/runner.py` |
| web orchestration | `caribou/src/caribou/server/streaming_runner.py` |
| agent topology | `caribou/src/caribou/agents/AgentSystem.py` and JSON blueprints |
| model clients | `caribou/src/caribou/core/` |
| execution memory/reports/artifacts | `caribou/src/caribou/execution/` |
| web sessions and persistence | `caribou/src/caribou/server/session_*.py` and routes |
| sandbox/container runtime | `caribou/src/caribou/sandbox/` |
| web API/events | `caribou/src/caribou/server/` |
| Angular client | `frontend/` |
| benchmark protocols | `benchmarking/` |

Inspect the code again before relying on this map; it describes a baseline, not a
permanent architecture.

## Known risks to avoid reinforcing

- CLI and web use parallel orchestration paths with different memory, metrics,
  reporting, artifact, and model-default behavior.
- Some web request fields are represented but do not control equivalent runtime
  semantics.
- Persisted web events allow display reconnection but do not reconstruct live
  agent, sandbox, or AnnData computation.
- Slurm benchmark scripts do not mean that web analyses are scheduler-backed.
- Offline sandbox values represented in a model/UI do not guarantee runtime
  support.
- Session ID access and proxy prefixes are not authentication or ownership.
- Direct JSON writes and process/background-thread execution are insufficient for
  durable distributed lifecycle state.

## Target boundaries

```text
CLI ───────┐
Web/API ───┼─► shared ExperimentSpec and application service
Benchmark ─┘                 │
                             ├─► lifecycle/event/provenance store
                             ├─► local or scheduler executor
                             ├─► AgentSystem/model/RAG/tools
                             └─► sandbox/checkpoint/artifact store
```

## Shared invariants

- One resolved run spec has the same scientific meaning through every interface.
- Model labels resolve to exact identifiers before execution.
- Topology, prompt, tools, RAG, memory, budgets, and evaluator are recorded.
- Every attempt has a stable ID and immutable terminal outcome.
- Events are ordered, versioned, replayable, and separate from current-state
  snapshots.
- Artifacts are typed, hashed, lineage-linked, and ownership-scoped.
- Checkpoints declare exactly what can be resumed and under which versions.
- Scheduler jobs are reconciled after service/client restart.
- Authorization is enforced server-side, not inferred from UI visibility.
- Security claims describe tested network, filesystem, identity, secret, process,
  and scheduler boundaries.

## Definition of done for architectural changes

- shared owner and adapters identified
- state/schema migration tested
- backward compatibility or break documented
- machine and human interfaces tested
- negative lifecycle paths tested
- provenance/evidence impact documented
- operator and user behavior updated
- no stronger claim made than validation supports
