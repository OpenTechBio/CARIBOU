# CARIBOU study-release layout

Adapt names to the repository, but preserve these responsibilities:

```text
release/
├── manifest.json
├── checksums.sha256
├── CITATION.cff
├── LICENSES/
├── software/          source identity, locks, build recipes
├── containers/        recipes, URI/digest metadata
├── configuration/     prompts, blueprints, RAG/tools, schemas
├── data/              accession/license/hash/transform manifests
├── experiments/       frozen specs and approval/policy records
├── runs/              every attempt, events, failures, telemetry, artifacts
├── analysis/          evaluators, aggregation, statistics
├── figures/           source tables, scripts, rendered panels, manifest
├── manuscript/        TeX, bibliography, supplements, PDF, build log
├── reproduction/      instructions, commands, report, comparisons
└── claims/            claim-evidence index and limitations
```

## Release manifest fields

- release ID/version and creation timestamp
- repository/commit/tag and dirty-state declaration
- parent candidate when superseding a release
- file path, SHA-256, size, media/schema type
- producer command/code identity
- inputs/lineage and associated experiment/run/figure/claim IDs
- public/private/controlled classification
- license and retention policy

## Clean replay gates

- fresh checkout/archive extraction
- no undeclared files from developer workspace
- dependency/container creation from released definitions
- dataset/model acquisition verifies exact identities
- representative run succeeds or fails only within declared tolerance
- aggregates and figures regenerate from released records
- manuscript compiles and matches the candidate
- all deviations and unavailable external services recorded

## External-action boundary

Preparing commands and artifacts is reversible local work. Pushing tags, opening
PRs, uploading archives, minting DOIs, sending messages, and submitting to a journal
are external actions and require explicit policy authority.
