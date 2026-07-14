# External-agent CLI contract

CARIBOU exposes a non-interactive lifecycle for Codex, Claude, scripts, and
other long-running callers. Non-streaming commands emit exactly one JSON object
on stdout. Event reads emit resumable JSON Lines. Semantic failures use the
stable exit codes reported by `capabilities`.

The shortest validated journey uses the deterministic control-plane probe:

```bash
caribou capabilities --json
caribou schema experiment --json
caribou experiment validate examples/experiments/lifecycle-smoke.yaml --json
caribou experiment plan examples/experiments/lifecycle-smoke.yaml --json
caribou experiment submit examples/experiments/lifecycle-smoke.yaml \
  --idempotency-key lifecycle-smoke-001 --json
```

Submission returns after durably queuing the run and launching a detached
worker. Preserve the returned `run_ids[0]`, then reconnect from any later CLI
process:

```bash
caribou run status RUN_ID --json
caribou run events RUN_ID --after 0 --format jsonl
caribou artifact list RUN_ID --json
caribou artifact verify RUN_ID --json
caribou artifact fetch RUN_ID ARTIFACT_ID --output ./result.json --json
```

Use the last event cursor as the next `--after` value to avoid duplicates.
Cancellation is durable and idempotent:

```bash
caribou run cancel RUN_ID --reason "caller stopped the experiment" --json
```

Agent runs can also stop cooperatively at the next completed-turn boundary:

```bash
caribou run checkpoint RUN_ID \
  --idempotency-key checkpoint-RUN_ID-turn-2 \
  --reason "preserve this completed analysis turn" --json
caribou run checkpoints RUN_ID --json
```

The worker finishes the current model response and its declared actions before
publishing the checkpoint. It then preserves the source attempt as terminal
`resumable`; it does not begin another model turn. A complete checkpoint links
hash-verified dataset, full message history, runner/agent state, executed-action
ledger, and artifact-frontier components. Resume creates a new immutable attempt
rather than reopening the source:

```bash
caribou run resume RUN_ID --from-checkpoint latest \
  --idempotency-key resume-RUN_ID-turn-2 --json
```

Retrying the exact resume request returns the same child and does not duplicate
workload execution. Local workers must claim the one durable process handle before
changing run state, invoking a model, or executing generated code. Selecting the
same checkpoint with another key fails because the initial slice does not support
branching. The child retains the frozen experiment,
model, code, container, budget, and total turn limit; it binds the checkpointed
AnnData artifact at `/workspace/dataset.h5ad`, restores the full-history cursor and
current agent, and starts with the next logical turn. Historical resumable attempts
remain in the ledger but do not prevent a successful leaf attempt from completing
the experiment.

This recovery contract currently supports only full-history memory and one
cooperative checkpoint per attempt. It is not a Python-process snapshot: only the
declared AnnData global `adata`, messages, runner counters, current agent, action
ledger, and artifact frontier are restored. A hard loss during a provider call,
between a provider receipt and durable response content, or midway through code
execution is ambiguous and is not automatically replayed. Scheduler preemption,
SIGKILL, episodic/report memory, arbitrary Python globals, and branching require
separate validation before they can be claimed.

Retry submission with the same idempotency key and unchanged specification to
recover the original experiment and run IDs without launching another worker.
Reusing the key with different content fails with a typed conflict. A caller
that records the plan hash may also pass `--expected-plan-hash` at submission
to reject configuration drift.

The lifecycle smoke adapter validates only local submission, persistence,
events, cancellation, and artifact handling. It does not invoke a model,
container, biological analysis, or Slurm.

The separately tested `agent_path_smoke` adapter crosses the real control
service, detached worker, existing agent runner, blueprint delegation, command
parser, event journal, and artifact store. Its model client and sandbox are
deterministic test boundaries: it does not validate an external model,
Apptainer/Singularity, biological correctness, or scheduler execution.

The initial `caribou_agent` adapter is implemented for a deliberately narrow
real pilot. It requires a clean exact Git commit, one hash-pinned local input,
a hash-pinned Apptainer/Singularity image, network-disabled generated code,
full-history memory, no RAG or external blueprint tools, no ambient cache or
custom mounts, and an external OpenAI or DeepSeek model identified
by exact model ID. Finite budget limits, model tuning fields, declared
container-runtime versions, retry policies, and implicit CellTypist caches are
rejected rather than ignored. Provider requests receive the remaining frozen
session deadline. The experiment metric definitions remain preregistered
metadata; this adapter does not execute their evaluator artifacts or enforce
the declared local CPU, memory, and storage maxima.

When the worker remains alive through receipt persistence, every completed or
failed OpenAI-compatible SDK attempt made by this adapter produces a
hash-verifiable `provider_call_receipt` artifact before response content is
used. Query its strict consumer contract with:

```bash
caribou schema provider-call-receipt --json
```

Receipts contain only whitelisted provider and request identifiers, the
requested and provider-returned model names, timing, finish status, and token
counts when supplied by the provider. They never contain prompts, response
content, raw error text or bodies, headers, URLs, clients, or credentials.
Missing usage remains `null`, never zero. Provider cost is explicitly
unavailable, and the run's budget counters are not yet reconciled from these
receipts. Do not infer resource enforcement, billed cost, evaluator execution,
or biological validity from a successful run. A hard process or node loss
during a provider request can leave a billed request without a receipt; this
slice does not yet implement pre-call intent logging or crash reconciliation.

Query `capabilities` rather than assuming later execution surfaces are
validated.
