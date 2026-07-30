# CARIBOU program-state contract

## Authoritative files

| Path | Purpose |
|---|---|
Resolve every path in this table beneath `CARIBOU_PROGRAM_HOME`.

| Relative path | Purpose |
|---|---|
| `policy.yaml` | User-granted authority and resource ceilings; agents must not broaden it |
| `state.json` | Current program/milestone/task state and next action |
| `decisions.jsonl` | Append-only material decisions and rationale |
| `blockers.json` | Active and resolved external/policy blockers |
| `experiments/index.json` | Experiment IDs, specs, conditions, and aggregate state |
| `runs/index.json` | Attempt IDs and terminal outcomes |
| `evidence/index.json` | Claim-to-artifact/evaluator links |
| `reviews/index.json` | Independent review records |
| `releases/index.json` | Candidate release identities and validation state |
| `manuscript/` | Private manuscript, strategy, repository map, and revision working tree |

Use `repo:path` for public-repository references and `program:path` for private
program-root references in machine state.

`state.json.documents` is the discovery map for manuscript and planning material.
Public skills must resolve these references instead of hardcoding a public
`manuscript/` path.

## State invariants

- Use UTC RFC 3339 timestamps.
- Use stable unique IDs; never recycle an ID after failure or cancellation.
- Preserve history in decision, run, blocker, and review records.
- Reference repository-relative paths or immutable external URIs.
- Record content hashes for evidence/release artifacts.
- Update `updated_at` and `next_action` after every meaningful transition.
- Allow at most one program task to be the immediate `next_action`.
- Require evidence links and independent reviewer ID for `Validated`.
- Require manuscript claim removal/link for `Not claimed`.
- Use `Blocked` only for an actual policy, credential, allocation, license, or
  unavailable external dependency after safe alternatives are exhausted.
- Before any remote Git write, require a non-`main` branch allowed by
  `policy.yaml.repository.remote_write_policy`; branch pushes do not imply tag,
  pull-request, merge, release, or submission authority.

## Decision-record fields

- `decision_id`, `timestamp`, `actor`, `context`
- `decision`, `alternatives`, `rationale`
- `policy_basis`, `evidence`
- `affected_tasks`, `reversible`
- `follow_up`

## Resume check

1. Parse all state files.
2. Compare HEAD/worktree to recorded code identity.
3. Reconcile active run IDs with processes/scheduler/output artifacts.
4. Verify last completed task evidence.
5. Recalculate remaining budgets.
6. Select or confirm `next_action`.
7. Record any correction as a decision.
