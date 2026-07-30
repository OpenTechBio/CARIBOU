---
name: operate-caribou-experiment
description: Validate, launch, monitor, cancel, resume when supported, and collect CARIBOU experiments through the CLI or approved Slurm workflow while preserving budgets, provenance, failures, and artifacts. Use when a human or long-running agent such as Codex or Claude is asked to run CARIBOU analyses or benchmarks, inspect run status, recover an interrupted run, or retrieve experiment outputs.
---

# Operate CARIBOU Experiment

Operate only capabilities discovered in the checked-out CARIBOU version. Never
pretend that a proposed asynchronous control-plane command exists.

## Discover capabilities

1. Locate the repository root and record branch, commit, and worktree status.
2. Inspect the installed and source CLI:

   ```sh
   caribou --help
   caribou run --help
   caribou run auto --help
   ```

3. Read [references/cli-capabilities.md](references/cli-capabilities.md) to
   distinguish the audited baseline from the target interface.
4. Prefer machine-readable capability/schema discovery when the current CLI exposes
   it. Otherwise, use only commands confirmed by `--help` or source inspection.
5. Read the experiment specification and identify unresolved `BLOCKED` values.

If the requested lifecycle cannot be performed reliably by the current interface,
state the limitation and propose or implement the missing platform behavior only
when the user authorized development.

## Preflight the run

Before any state-changing execution, verify:

- exact code commit and dirty-state policy;
- dataset/reference/resource paths, permissions, versions, and hashes;
- blueprint, driver, prompt, turns, RAG/tools, memory, and evaluator settings;
- model provider and exact model identifier resolved by this interface;
- sandbox/container backend, image, mounts, network, and output path;
- CPU/GPU/memory/wall-time/storage/concurrency request;
- live-policy authorization plus planned API calls/tokens/currency and scheduler
  resources for consumption accounting;
- retry, timeout, cancellation, checkpoint, and failure rules; and
- stable experiment/run naming and a unique output directory.

Run a dry-run, validation, or tiny pilot first when available. Continue
autonomously within the live policy, including when its model, spend, or Slurm
ceiling is explicitly unbounded; stop only when the action exceeds policy or a
credential, license, safety boundary, or executable resource is actually missing.

## Launch without losing control

Prefer the versioned experiment interface when capability discovery confirms it:

```text
experiment validate → experiment plan → experiment submit
run status/events/wait/cancel/resume → artifact list/fetch/verify
```

For the current foreground `caribou run auto` interface:

- supply every available option explicitly rather than accepting interactive
  prompts or drifting defaults;
- use a dedicated output directory and request reports/benchmark records when
  appropriate;
- capture the fully resolved invocation and environment without exposing secrets;
- keep a tool/session handle for long-running execution and poll it at reasonable
  intervals;
- send concise progress updates at least every 60 seconds while actively managing
  the run; and
- do not claim durable detach/reconnect, stable run IDs, or resume unless an
  external scheduler or implemented control plane actually provides it.

For Slurm, use `create-traceable-slurm` to lock parameters in submitted scripts and
`submit-sbatch-dag` to validate, submit, monitor, and repair dependency workflows.
Require `#SBATCH --partition=peerd` (or the equivalent locked `--partition=peerd`)
in every CARIBOU job, reject all other partitions and environment overrides, and
record the resolved partition plus job and array IDs in the run ledger.

## Monitor and respond

- Treat status reads as non-mutating.
- Preserve ordered events/logs and record the last observed cursor or timestamp.
- Distinguish queued, starting, running, checkpointed, cancelling, cancelled,
  failed, resumable, rejected, and succeeded states when the backend supports them.
- Do not infer success from a vanished process or scheduler job.
- On transient failure, consult the declared retry policy and live policy before
  retrying; always record actual consumption even under an unbounded ceiling.
- On protocol, security, data-license, budget, or biological-integrity failure, stop
  and escalate.
- Never delete or overwrite a failed attempt when resuming or retrying.
- Use cooperative cancellation first; verify the terminal state and partial
  artifacts afterward.

## Collect evidence

For every attempt, preserve or reconstruct as far as the current version permits:

- experiment/run ID, condition, replicate, owner, timestamps, and terminal state;
- Git commit, package/container/runtime versions, model resolution, and data hashes;
- resolved prompt/blueprint/tools/RAG/memory/evaluator configuration;
- command/job script, scheduler IDs/states, node/resources, runtime, memory, and GPU;
- model usage, cost, messages, turns, delegations, code, errors, and corrections;
- notebooks, transformed data references, metrics, reports, checkpoints, logs, and
  artifact hashes; and
- every failure, retry, cancellation, exclusion, and rationale.

Use `audit-caribou-evidence` before promoting collected outputs to validated
manuscript evidence.

## Finish the operation

Report:

1. experiment and run identifiers or the best available output identity;
2. exact submitted configuration and execution backend;
3. current or terminal state;
4. budget/resources consumed;
5. failures, retries, cancellations, and deviations;
6. artifact locations and verification status; and
7. whether the result is only operational, `Evidence collected`, ready for audit,
   or blocked.
