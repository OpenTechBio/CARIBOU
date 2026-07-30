# CARIBOU experiment specification reference

Use this reference to draft planning specifications. Do not imply that fields are
implemented CLI options until capability inspection confirms them.

## Identity and purpose

- schema version, spec ID, spec version, title
- exploratory/pilot/confirmatory classification
- biological or systems question
- hypothesis and null/negative interpretations
- owner, scientific reviewer, infrastructure reviewer
- decision or manuscript claim affected

## Immutable inputs

- Git repository, branch, commit, dirty-state policy
- Python package and frontend versions
- container URI and digest; runtime and version
- model provider, exact model identifier, date, parameters, context limit
- local model artifact, quantization, server version, host hardware
- dataset accession, version, license, approved path, content hash
- transformations, subsets, reference datasets, and label dictionaries
- blueprint, prompt, RAG corpus, code-sample, tool-policy, and evaluator hashes

## Design

- conditions and one intended intervention
- controlled variables
- independent replication unit
- replicate count and seed/request-ID policy
- randomized or blocked run order
- task and resource budgets held equal across conditions
- primary endpoint and decision threshold
- secondary/exploratory endpoints
- denominator, missingness, exclusion, retry, and stopping rules
- aggregation and uncertainty method

## Execution

- local or scheduler executor
- cluster, partition/account/QoS, node class; CARIBOU jobs must lock partition
  `peerd` in both the specification and rendered submission script
- CPU, GPU, memory, wall time, scratch, storage, concurrency
- network and credential boundary
- maximum API calls, tokens, and currency
- checkpoint stages and resume compatibility policy
- timeout, cancellation, and retry policy
- expected output and artifact roots

## Evidence

- run manifest schema
- ordered event schema
- metric, failure, telemetry, checkpoint, artifact, and budget records
- raw-to-aggregate code path
- figure-source tables and scripts
- hash/signature verification
- retention and release destination
- independent review and reproduction plan

## Gates

- preflight validation requirements
- pilot acceptance/rejection thresholds
- authorization required for full execution
- conditions that block, narrow, or remove a claim
- criteria for `Evidence collected` and `Validated`
