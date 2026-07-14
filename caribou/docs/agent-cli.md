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
