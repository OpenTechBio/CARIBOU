---
name: design-caribou-experiment
description: Design frozen, evidence-ready CARIBOU biological, benchmark, ablation, robustness, local-model, scaling, and HPC experiments. Use when planning a new CARIBOU analysis, selecting conditions or datasets, defining an ExperimentSpec, preparing a pilot or confirmatory study, estimating replication and budgets, or checking a design for confounding before execution.
---

# Design CARIBOU Experiment

Design experiments that can survive scientific review and be executed by either a
human or a software agent without inventing missing parameters.

## Establish context

1. Locate the repository root with `git rev-parse --show-toplevel`.
2. Resolve `CARIBOU_PROGRAM_HOME`, read `state.json`, and load the relevant
   `documents` entries. Resolve `program:` beneath the private program home and
   `repo:` beneath the public repository root; never assume the manuscript is in
   the public checkout.
3. Inspect the actual blueprints, prompts, runner options, benchmark definitions,
   evaluators, datasets, and ignored-result boundaries involved in the request.
4. Treat planning documents as constraints and source code as the authority for
   currently implemented behavior.

## Classify the study

Label every run before execution:

- **Exploratory:** may discover hypotheses or tune procedures; never present it as
  confirmatory evidence.
- **Pilot:** tests feasibility, telemetry, schemas, and acceptance thresholds before
  expensive execution.
- **Confirmatory:** uses a frozen specification, endpoints, exclusions, and analysis
  plan.

Do not relabel a run after inspecting its outcome.

## Build the specification

Use [references/experiment-spec.md](references/experiment-spec.md) for required
fields. Copy [assets/experiment-spec.template.yaml](assets/experiment-spec.template.yaml)
when a concrete spec artifact is requested.

Define, at minimum:

1. question, hypothesis, study class, owner, reviewer, and decision affected;
2. exact code commit, container, environment, models, datasets, prompts,
   blueprints, RAG corpus, tools, and code samples;
3. conditions, intended intervention, controlled variables, replication units,
   seeds, and run order;
4. primary and secondary endpoints, evaluator versions, denominators, uncertainty,
   exclusions, and stopping rules;
5. CPU/GPU/memory/time/storage/concurrency and external-model budgets;
6. output, event, failure, telemetry, checkpoint, and artifact requirements;
7. pilot acceptance gate, confirmatory entry gate, and claim consequences.

Use content hashes or immutable identifiers wherever possible. Treat the live
policy as preauthorization within its stated limits. Mark only inputs or authority
that are genuinely unavailable under that policy `Blocked`; never guess
credentials, cluster facts, model snapshots, dataset licenses, or biological
ground truth.

## Control confounding

For matched single-agent versus multi-agent comparisons, hold constant:

- exact model and provider parameters;
- analytical instructions and task prompt;
- dataset and reference resources;
- code samples, RAG collection, and tool permissions;
- persistent state and execution environment;
- context and turn/token budgets;
- retry, timeout, and failure rules; and
- evaluator and aggregation code.

Vary only topology, role separation, and delegation. If the current runners cannot
hold these factors constant, record the study as blocked or redesign the execution
path before interpreting the comparison.

## Design for failures and negative results

- Preserve every attempt, including rejection, cancellation, timeout, preemption,
  and exclusion.
- Predefine the failure taxonomy and whether each outcome enters the denominator.
- Measure silent biological error separately from process failure.
- State what conclusion follows if the intervention wins, ties, loses, or cannot be
  executed reliably.
- Prefer a useful negative conclusion over post-hoc benchmark changes.

## Gate execution

Do not launch work outside `$CARIBOU_PROGRAM_HOME/policy.yaml`. Within an
unbounded live authorization, record actual model, spend, scheduler, and data
consumption rather than requesting another human approval. Every CARIBOU Slurm
specification and submitted script must use partition `peerd`; reject any other
partition. Compose with `create-traceable-slurm` and `submit-sbatch-dag` rather
than duplicating their scheduler procedures.

Before declaring the design ready, verify:

- the pilot is smaller than the proposed full study;
- resource and spending ceilings are explicit;
- the run and result schemas are frozen;
- every quantitative claim has a planned machine-readable record;
- every planned figure has source data and a versioned generation path; and
- an independent reviewer can identify the intervention and replication unit.

## Deliver the design

Return or write:

1. the versioned experiment specification;
2. a concise confound audit;
3. a resource/budget estimate with assumptions;
4. the pilot plan and acceptance gate;
5. the full matrix, blocked inputs, and any policy authority still required; and
6. the claim-to-outcome decision table.

Use only `Pending`, `In progress`, `Evidence collected`, `Validated`, `Not
claimed`, and `Blocked` for tracked work items.
