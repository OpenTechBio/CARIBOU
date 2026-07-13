# CARIBOU CLI capability reference

Always confirm capabilities from the checked-out source or `--help`; this reference
records the 2026-07-13 baseline and intended direction.

## Audited baseline

The Typer CLI exposes these groups:

- `create-system`
- `datasets`
- `run`
- `config`
- `utils`
- `serve`
- `server`

`caribou run auto` accepts blueprint, driver, dataset/reference/resources, model
backend, Ollama host, Docker/Singularity backend, output directory, prompt, turn
count, benchmark module/ID, memory settings, and session-report options. It is a
foreground run interface rather than a durable asynchronous experiment service.

Do not assume the baseline provides:

- stable experiment/run IDs;
- JSON/JSONL machine output;
- submit/status/event/cancel/resume lifecycle commands;
- scheduler-backed execution from the web or CLI control plane;
- atomic durable lifecycle storage;
- computation recovery from persisted checkpoints; or
- automatic CLI/web scientific parity.

Some capabilities may have been implemented after this baseline. Discover them
rather than suppressing them based on this document.

## Target control-plane capabilities

The program goal proposes:

```text
caribou capabilities --json
caribou schema experiment --json
caribou experiment init|validate|plan|submit|compare
caribou run status|wait|events|cancel|resume
caribou artifact list|fetch|verify
caribou scheduler inspect
caribou doctor --json
```

Use these only when command discovery proves they exist in the active version.

## Automation-quality checks

- non-interactive mode never prompts
- stdout contains only the documented JSON object or JSON Lines
- diagnostics use stderr
- exit codes distinguish failure categories
- mutation is idempotent or duplicate-protected
- output includes schema/version/commit/object ID/state/timestamp
- event following can restart from a cursor
- state survives client and service restart where claimed
- cancellation and resume produce durable records
- secrets do not appear in commands, logs, events, or artifacts
- paths, ownership, budgets, and resources are validated before execution
