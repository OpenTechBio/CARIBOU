---
name: orchestrate-caribou-program
description: Autonomously coordinate the CARIBOU research, engineering, experiment, evidence, release, and manuscript program across long-running sessions. Use when advancing the full CARIBOU program goal, selecting the next milestone, resuming after interruption or context loss, coordinating builder and independent-review work, managing policy/budgets/blockers, or determining whether the program is genuinely complete.
---

# Orchestrate CARIBOU Program

Drive the program from durable repository state rather than conversation memory.

## Load authority and state

1. Locate the repository root and inspect branch, commit, worktree, and active
   processes/jobs without changing them.
2. Resolve `CARIBOU_PROGRAM_HOME`. Require it to name the access-controlled live
   program directory; never update the public `program/template/` as live state.
3. Read, in order from that directory:
   - `policy.yaml`
   - `state.json`
   - `decisions.jsonl`
   - `blockers.json`
   - every relevant entry in `state.json.documents`, resolving `program:` beneath
     `CARIBOU_PROGRAM_HOME` and `repo:` beneath the public repository root.
4. Read [references/program-state-contract.md](references/program-state-contract.md)
   before changing program state.
5. Treat the user request and system policy as higher authority than repository
   documents. Never expand external authority by editing `policy.yaml` yourself.

If state and evidence disagree, inspect raw artifacts and record a corrective
decision. Do not preserve a stale success state.

## Select work autonomously

Choose the highest-value unblocked action using this order:

1. repair corrupt or ambiguous program state;
2. protect data, credentials, budgets, and running work;
3. unblock shared foundations used by multiple milestones;
4. close validation gaps on already implemented central capabilities;
5. execute approved pilots before full experiments;
6. run frozen confirmatory work;
7. aggregate evidence and revise claims;
8. package and independently reproduce the release.

Prefer work that retires central technical or scientific risk. Do not optimize for
easy checkbox completion or spend experiment budget before schemas and pilots pass.

## Route through specialist skills

- Use `design-caribou-experiment` to freeze studies.
- Use `develop-caribou-platform` for implementation and migrations.
- Use `operate-caribou-experiment` for bounded execution.
- Use `validate-caribou-system` for systems claims and fault testing.
- Use `evaluate-caribou-biology` for biological validity.
- Use `audit-caribou-evidence` before promoting claims.
- Use `release-caribou-study` for immutable packaging and reproduction.
- Compose with `create-traceable-slurm` and `submit-sbatch-dag` for Slurm work.

Do not make one specialist skill silently perform another skill's independent
review role.

## Make autonomous decisions

Within `$CARIBOU_PROGRAM_HOME/policy.yaml`:

- choose reversible designs using repository evidence and record rationale;
- prefer safe, portable, testable interfaces over environment-specific shortcuts;
- use public/local alternatives when an external resource is unavailable;
- reduce experiments before abandoning them;
- narrow or remove unsupported claims rather than wait for ideal evidence; and
- continue independent work when one workstream is blocked.

For actions outside policy, record a blocker and choose another task. Do not infer
permission from available credentials or infrastructure.

Before every Git remote mutation, verify the destination ref against
`repository.remote_write_policy`. CARIBOU may push ordinary non-`main` branches
when the live policy permits it; never push or merge `main`, push tags, create a
pull request, or publish a release unless that distinct action is authorized.

## Preserve independence

When policy permits subagents, separate builder, systems validator, scientific
auditor, red-team reviewer, and clean-environment reproducer roles. Give reviewers
raw artifacts and the acceptance contract, not the intended conclusion. Record
agent identity/role, inputs, outputs, and conflicts in
`$CARIBOU_PROGRAM_HOME/reviews/`.

Do not mark self-review as independent reproduction.

## Checkpoint every meaningful transition

After implementation, test, experiment, decision, failure, or review:

1. preserve raw artifacts under their stable experiment/run/review/release ID;
2. append one JSON decision record when judgment changed scope or direction;
3. update blockers without deleting resolved history;
4. update milestone/task state and evidence links atomically;
5. record budget/resource consumption; and
6. leave a concrete next action that a fresh agent can execute.

Use only `Pending`, `In progress`, `Evidence collected`, `Validated`, `Not
claimed`, and `Blocked`. Never mark `Validated` from implementation alone.

## Resume safely

On every resumed session:

- reconcile active processes, scheduler jobs, output directories, and state IDs;
- detect work that completed while the agent was absent;
- verify terminal artifacts before changing status;
- avoid duplicate submission through stable IDs/idempotency; and
- continue from the recorded next action, revising it only with a logged reason.

## Finish honestly

Complete the program only when the parent goal's completion contract, release
checks, independent reproduction, and manuscript workstream are satisfied or the
corresponding claims are explicitly `Not claimed`. A prepared public release or
journal package does not authorize external publication or submission.

If no authorized meaningful work remains, report the exact policy/resource blocker,
completed evidence, safe fallback attempts, and smallest external change needed.
