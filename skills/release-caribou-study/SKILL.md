---
name: release-caribou-study
description: Assemble, verify, and reproduce an immutable CARIBOU software, experiment, evidence, figure, and manuscript release. Use when preparing a release candidate, archival package, DOI-ready deposit, clean-environment replay, figure-source bundle, submission-ready manuscript, or final reproducibility and availability audit. This skill prepares external releases but does not authorize publication or journal submission.
---

# Release CARIBOU Study

Build a release that an independent agent or researcher can inspect and reproduce
without relying on ignored local state, mutable branches, undocumented credentials,
or developer memory.

## Establish release authority

1. Resolve `CARIBOU_PROGRAM_HOME` and read its `policy.yaml`; distinguish permission
   to prepare from permission to publish, archive, or submit. Never infer authority
   from the public template.
2. Read the program/revision completion contracts and claim-evidence index.
   Resolve manuscript and planning sources through `state.json.documents`; all
   `program:` references are private-program-home relative.
3. Inspect branch, commit, worktree, tags, ignore rules, large/local artifacts,
   dependency locks, container definitions, manuscript source, and build tools.
4. Read [references/release-layout.md](references/release-layout.md) and freeze a
   candidate release ID before copying or aggregating evidence.

Do not discard unrelated worktree changes, rewrite history, create public releases,
mint DOIs, or submit manuscripts without explicit policy authority.

## Freeze identities

Record exact:

- Git commit/tag and source-tree hash;
- package versions and dependency locks;
- container URI, digest, build recipe, runtime, and platform;
- models, dates, parameters, local artifacts/quantization, and provider policy;
- datasets, accessions, versions, licenses, transformations, paths/URIs, and hashes;
- prompts, blueprints, RAG corpus, code samples, tools, evaluators, and schemas;
- cluster/scheduler/hardware/deployment configuration; and
- experiment, run, failure, review, aggregate, figure, and manuscript IDs.

Fail explicitly on mutable or unknown identities required by a claim.

## Assemble evidence

- Include every declared attempt and terminal outcome.
- Verify run counts, denominators, exclusions, retries, costs, and resource totals.
- Include raw machine-readable records and deterministic aggregation code.
- Regenerate tables and figures from released source records in a clean output
  directory; compare hashes or declared numeric/visual tolerances.
- Include architectural illustrations separately from empirical figure sources.
- Resolve ignored/local evidence by either curating it into the release with
  provenance or removing the dependent claim.
- Produce a claim-evidence index that links manuscript statements/panels to exact
  records and generation commands.

## Reproduce independently

Use an isolated environment and, when authorized, an independent agent that did
not build the candidate. Supply only the candidate package and documented access
procedure. Record environment creation, downloads/hashes, commands, deviations,
failures, outputs, metrics, figures, and comparison tolerances.

Repair package/documentation defects, create a new candidate version, and repeat
affected steps. Never edit the reproduction report to hide setup failure.

## Build the manuscript package

- Verify exact model/dataset/run labels and consistency across Methods, Results,
  captions, supplements, and availability statements.
- Run word-count and manuscript-audit tools from documented paths.
- Compile from a clean environment and preserve the log.
- Inspect every page for references, clipping, fonts, rasterization, labels,
  legends, accessibility, and supplemental order.
- Confirm every claim is `Validated` or removed/marked `Not claimed`.
- Ensure public instructions omit credentials, private hostnames, controlled data,
  and security-sensitive configuration.

## Verify package integrity

Create a machine-readable release manifest with relative path, size, media/schema
type, producer, lineage, and SHA-256 for every file. Verify it after packaging and
again from the isolated reproduction copy. Record licenses, citation metadata,
retention, public/private split, and known limitations.

Use `audit-caribou-evidence` for the final independent claim audit. A successful
build alone is not a validated release.

## Deliver without overstepping

Produce the candidate directory/archive, manifest and checksums, reproduction
report, claim-evidence index, release notes, manuscript/PDF, and exact proposed
external commands. If publication authority is false, stop at “release-ready.” A
separately authorized non-`main` branch push does not authorize upload, DOI, pull
request, tag, merge, release, or submission actions.
