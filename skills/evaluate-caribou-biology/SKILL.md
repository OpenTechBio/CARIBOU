---
name: evaluate-caribou-biology
description: Design, run, and review biological validity evaluation for CARIBOU single-cell analyses, including QC, integration, cell typing, differential expression, metadata reasoning, silent-error detection, standard pipelines, external benchmarks, distribution shift, and downstream error propagation. Use when CARIBOU outputs must be judged biologically rather than only structurally or operationally.
---

# Evaluate CARIBOU Biology

Judge whether an analysis is biologically defensible, not merely whether it
terminated or populated expected AnnData fields.

## Define the biological question

1. Record tissue, organism, disease/state, platform, assay, cohort, and intended
   scientific use.
2. Verify dataset accession/version/license, preprocessing, labels, mappings,
   splits, leakage risks, and content hashes.
3. Define the analytical decision being evaluated and plausible biological failure
   modes.
4. Select primary and secondary endpoints before inspecting confirmatory results.
5. Read [references/metrics-and-baselines.md](references/metrics-and-baselines.md)
   for task-specific considerations.

Do not infer ground truth from CARIBOU's own outputs. Mark uncertain or harmonized
labels and preserve the original vocabulary.

## Choose meaningful comparators

Include, where feasible:

- a deterministic or expert-designed standard pipeline using frozen versions;
- a matched single-agent CARIBOU condition;
- the full multi-agent condition when topology is under study; and
- an external agent/benchmark only when licensing, inputs, tools, and evaluation
  can be made meaningfully comparable.

Hold data, model, prompt/resources, tools, budgets, environment, and evaluator
constant for architectural comparisons. Do not attribute scaffolded execution or
model differences to multi-agent delegation.

## Evaluate beyond structure

- Verify raw counts, normalized layers, embeddings, graphs, labels, and provenance
  before applying metrics.
- Measure per-dataset and per-run results; display distributions and failures.
- Use datasets/runs—not cells—as independent experimental units unless the design
  justifies otherwise.
- Report per-class and macro metrics for imbalanced labels.
- Evaluate biological conservation and batch removal separately.
- Inspect decisions such as filtering thresholds, normalization, covariates,
  integration method, resolution, marker evidence, and reference transfer.
- Test downstream consequences of upstream errors in persistent AnnData state.
- Distinguish confident wrong outputs from detected process failures.

## Test silent error and shift

Use predefined perturbations relevant to the task: missing fields, misleading
metadata, unexpected gene identifiers, ambiguous labels, rare populations, decoy
files, prompt bloat, corrupted inputs, platform/tissue/species shift, over-filtering,
and reference mismatch.

Record detection, correction, safe termination, uncertainty, silent error, and
downstream propagation. Never introduce a perturbation into controlled or original
data without a disposable copy and explicit policy authority.

## Separate evaluation roles

Keep analysis generation, metric computation, and evidence review logically
separate. When subagents are authorized, give the evaluator frozen outputs and
criteria without the producing agent's preferred interpretation.

Automated metrics and agent review do not equal blinded human-expert assessment.
If no human expert participates, report that limitation and do not claim expert
validation.

## Preserve evidence

For every attempt, retain exact inputs, resolved configuration, code/notebook,
AnnData checkpoints/references, metrics by class/dataset/run, failures, label maps,
resource/model usage, evaluator version, and artifact hashes. Keep exploratory
diagnostics separate from frozen confirmatory evaluation.

Use `audit-caribou-evidence` to verify denominators, confounding, source data, and
claim wording before manuscript use.

## Report conclusions

Report:

1. biological question, scope, datasets, and comparators;
2. metric rationale and limitations;
3. individual-run and dataset-level outcomes with uncertainty;
4. rare-class, failure, silent-error, and distribution-shift behavior;
5. where CARIBOU, a single agent, or a fixed pipeline is preferable;
6. unexpected or negative findings;
7. exact evidence maturity; and
8. claims supported, narrowed, or `Not claimed`.
