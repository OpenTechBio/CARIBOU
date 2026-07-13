# CARIBOU biological metrics and baselines

Choose metrics appropriate to the declared biological question and dataset. Do not
average unrelated endpoints into a composite without a predeclared justification.

## Quality control

- retained cells/genes and group-specific retention
- mitochondrial/ribosomal/count distributions
- doublet detection with known limitations
- rare-population loss and over-filtering
- sensitivity to thresholds and covariates
- downstream clustering/typing effects, not only presence of QC fields

## Integration and batch correction

- report batch removal and biological conservation separately
- use suitable SCIB-style metrics with exact versions/settings
- compare expression/embedding/neighborhood fidelity
- assess cluster/label conservation and rare populations
- disclose reference labels and batch/biology confounding
- include unintegrated and standard-method baselines

## Cell typing

- exact label vocabulary and harmonization dictionary
- per-class precision, recall, F1, support, and confusion matrix
- macro and weighted summaries
- unknown/ambiguous handling and calibration/uncertainty when available
- rare-class and tissue/platform/species shift
- reference-based and standard marker-based baselines

## Differential expression

- define biological replicate and contrast correctly
- avoid treating cells as independent replicates
- compare direction, rank, effect size, significance, and known markers
- test sensitivity to preprocessing, covariates, and pseudobulk strategy
- preserve complete result tables and multiple-testing method

## Metadata reasoning

- score direct calculations separately from semantic inference
- prevent identifier conventions or filenames from leaking labels
- report field-level metrics rather than only a composite
- distinguish data adequacy warnings from correct biological conclusions

## End-to-end analysis

- predeclare question and checkpoints
- score upstream decisions and downstream propagation
- compare fixed pipeline, matched single-agent, and multi-agent conditions
- include completion, silent error, corrections, turns, runtime, cost, and artifacts
- require dataset-level replication before generalization

## External validation

Prefer public benchmark splits and frozen evaluator code. Record any adaptation
needed to make tools, inputs, outputs, or budgets comparable. If equivalence is not
possible, present the external result as contextual rather than a leaderboard.
