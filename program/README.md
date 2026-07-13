# CARIBOU Autonomous Program Contract

This public directory contains the reusable contract for CARIBOU's long-running
research, engineering, experiment, evidence, release, and manuscript program. Live
operational state belongs in a separately access-controlled repository.

## Files

| Path | Purpose |
|---|---|
| `template/` | Safe complete example state to copy into a private program repository |
| `schemas/` | Machine-readable policy and state contracts |
| `validate_state.py` | Deterministic structural/state-integrity audit |
| `requirements.txt` | Validator dependency |

The repository's own live state is maintained in the private internal repository
at `dev/caribou_program/`. That path is an implementation detail of this checkout;
agents and tools must discover the live location from `CARIBOU_PROGRAM_HOME`.

## Configure live state

Create a private state directory by copying `program/template/`, then set:

```sh
export CARIBOU_PROGRAM_HOME=/absolute/path/to/private/caribou_program
```

Do not put secrets or raw controlled data in the program-state repository. Store
references to approved credential providers and governed data locations.

## Operating protocol

1. Resolve `CARIBOU_PROGRAM_HOME`; never treat the public template as live state.
2. Read its policy, state, decisions, and blockers at the start of every long-running
   or resumed turn.
3. Reconcile recorded work with Git, processes, scheduler state, and artifacts.
4. Execute only actions allowed by policy and higher-level system/user authority.
5. Append decisions; never rewrite historical decisions to improve the narrative.
6. Preserve every experiment/run/review/release attempt under a stable ID.
7. Update state after every meaningful implementation, test, experiment, failure,
   review, or claim decision.
8. Leave one executable `next_action` for a fresh agent.
9. Run the validator before handoff and release gates.

Validate the public template:

```sh
python program/validate_state.py --program-root program/template
```

Validate live private state:

```sh
python program/validate_state.py --program-root "$CARIBOU_PROGRAM_HOME"
```

Repository policy never overrides system rules, sandbox restrictions, credentials,
licenses, or an explicit newer user instruction. Agents must not broaden
`policy.yaml` merely to bypass an unavailable permission.

The public template is deliberately conservative. A private policy may grant
additional bounded authority, but ignored files and available credentials never
constitute permission.
