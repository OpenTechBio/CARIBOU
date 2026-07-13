# CARIBOU evidence-audit reference

## Release identity

- exact Git commit and clean/dirty state
- immutable tag/archive and DOI when claimed
- package versions and locked dependencies
- container URI, digest, runtime, and build inputs
- exact models, dates, parameters, and local artifact hashes
- dataset accessions, versions, licenses, transformations, and hashes
- prompt, blueprint, RAG, tool, code-sample, and evaluator hashes

## Run completeness

- stable experiment and run IDs
- declared condition and independent replication unit
- every attempt represented exactly once
- failures, retries, cancellations, exclusions, and reasons retained
- start/end times, executor, scheduler ID, container, node/resources
- model usage, tokens, cost, turns, delegations, errors, corrections
- artifact index and content hashes
- denominator reproducible from terminal outcomes and declared rules

## Comparative integrity

- intended intervention is explicit
- non-intervention inputs and budgets match
- run order/randomization and seeds/request IDs recorded
- endpoint, evaluator, exclusion, stopping, and aggregation rules frozen
- exploratory tuning separated from confirmatory evaluation
- uncertainty uses runs/datasets rather than individual cells as replicates
- negative and equivalent results remain visible

## Figure and manuscript integrity

- every number resolves to a machine-readable source row
- every panel has source data and a versioned generation script
- source run IDs and code commit appear in a figure manifest
- captions match conditions, replicates, spread, and exact model labels
- Methods, Results, figures, supplements, and availability statement agree
- architectural illustrations are not presented as empirical measurements

## Claim-specific minimums

| Claim | Minimum evidence |
|---|---|
| implemented | inspected code/configuration plus proportionate tests |
| operational | recorded end-to-end run and environment manifest |
| multi-agent benefit | matched controlled comparison with uncertainty and cost |
| HPC compatible | real scheduler/Apptainer run with job and resource records |
| scalable | declared size/concurrency sweep with runtime/resource/failure telemetry |
| local inference | end-to-end run with no external model API and exact local model artifact |
| CLI/web equivalent | same frozen spec, shared semantics, and compared output/provenance |
| robust/recoverable | predefined perturbations and verified checkpoint/resume outcomes |
| biologically valid | appropriate datasets, baselines, metrics, per-class results, and review |
| secure/private | threat model plus enforced auth, ownership, path, secret, network, and storage tests |
| reproducible | independent clean replay from immutable release within declared tolerances |

## Defensible wording

- Prefer “implements” or “supports” for code-only capability.
- Prefer “demonstrated on” for bounded operational evidence.
- State exact environment, task, model, and dataset limitations.
- Use “not evaluated” rather than implying a negative or positive result.
- Remove the claim when its evidence cannot be completed before release.
