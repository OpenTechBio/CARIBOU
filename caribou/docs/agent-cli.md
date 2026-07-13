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
container, biological analysis, or Slurm. Query `capabilities` rather than
assuming those later execution surfaces are validated.
