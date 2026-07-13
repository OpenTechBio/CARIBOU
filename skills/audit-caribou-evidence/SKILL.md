---
name: audit-caribou-evidence
description: Audit CARIBOU manuscript claims, benchmark results, figures, run ledgers, release manifests, provenance, and reproducibility evidence. Use when deciding whether a CARIBOU claim is supported, reviewing new experimental outputs, checking figure sources or replicate accounting, assessing submission readiness, or separating implemented features from validated capabilities.
---

# Audit CARIBOU Evidence

Evaluate evidence without manufacturing certainty or converting local artifacts
into release evidence by assumption.

## Establish the audit boundary

1. Locate the repository root and record branch, commit, worktree status, and tags.
2. Read the relevant claim, figure, result, or release request.
3. Resolve `CARIBOU_PROGRAM_HOME`, read `state.json`, and load the repository map
   and revision strategy from its `documents` references. Resolve `program:` under
   the private program home and `repo:` under the public repository root.
4. Identify which evidence roots are tracked, ignored, external, historical, or
   missing. Treat `dev/`, ignored benchmark results, local images, and untracked
   manuscript artifacts as local evidence until provenance and release inclusion
   are demonstrated.
5. Use [references/evidence-audit.md](references/evidence-audit.md) for the detailed
   chain and decision criteria.

Do not modify code, rerun experiments, regenerate figures, or rewrite claims when
the user requested only an audit. Read-only verification is allowed.

## Classify the claim

Classify each claim before evaluating it:

- **Implementation:** code/configuration for a capability exists.
- **Operation:** the capability ran once in a recorded environment.
- **Performance:** quantitative comparative or scaling effect.
- **Reliability/recovery:** behavior under declared failures or repeated runs.
- **Biological validity:** output quality under biological metrics or expert review.
- **Security/privacy:** enforced boundary validated against a threat model.
- **Reproducibility:** an independent clean replay from immutable inputs.

Require evidence appropriate to the claim type. Unit tests can support
implementation behavior; they cannot establish production security, HPC scaling,
biological validity, or independent reproducibility.

## Trace the evidence chain

For every quantitative or comparative statement, trace:

```text
claim or panel
  → aggregate/table row
  → declared run IDs and denominator
  → immutable run/result/failure records
  → exact evaluator and aggregation code
  → model/data/prompt/blueprint/container/software manifest
  → raw artifacts and content hashes
```

Break the chain at the first unsupported link. Do not fill missing links by
inferring likely model versions, selected runs, seeds, or dataset provenance.

## Check integrity

- Verify that all attempts are present, including failures, retries, cancellations,
  preemptions, and exclusions.
- Reconcile declared replicate counts with actual run IDs and denominators.
- Confirm that exploratory and confirmatory results are distinguishable.
- Check whether conditions differ in unintended model, prompt, tool, persistence,
  retrieval, context, budget, environment, or evaluator settings.
- Verify source-data hashes and that figure/table generation is scripted and
  versioned.
- Check Git tracking and ignore rules; a local file is not automatically part of an
  immutable release.
- Check exact model identifiers, evaluation dates, dataset accessions/versions,
  container digest, and code commit.
- Compare manuscript text, Methods, captions, labels, supplements, and source
  records for consistency.
- Treat “secure,” “private,” “scalable,” “equivalent,” “robust,” and
  “reproducible” as high-burden terms.

## Decide status

Use only these outcomes:

- `Validated`: the complete chain and interpretation were independently checked.
- `Evidence collected`: execution finished and raw artifacts exist, but review is
  incomplete.
- `In progress`: evidence generation or repair is active.
- `Not claimed`: the unsupported claim is removed or deliberately excluded.
- `Blocked`: a specific owner, permission, credential, artifact, or external
  decision is required.
- `Pending`: required work has not started.

Do not equate “no contradiction found” with `Validated`.

## Report the audit

Lead with the decision and provide:

1. claim or artifact audited;
2. status and confidence boundary;
3. evidence that directly supports it;
4. missing or inconsistent links;
5. exact corrective action or narrower defensible wording;
6. owner/approval needed for blocked items; and
7. files, commands, and hashes used in the audit.

Prefer a compact claim-evidence table for multiple claims. Clearly label any
inference and never quote a result that cannot be traced to its run set.
