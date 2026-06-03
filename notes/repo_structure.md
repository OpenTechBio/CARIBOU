# CARIBOU Repo Structure Notes

## Mental Model: Two Distinct Systems

---

## System 1: The CARIBOU Agent (the product)

**Root:** `caribou/src/caribou/`

This is the installable framework — what end users run. It has no knowledge of SLURM scripts, evaluation CSVs, or manuscript figures.

### Core Components

| Component | Path | Role |
|---|---|---|
| Agent blueprint loader | `agents/AgentSystem.py` | Parses JSON configs defining agents, commands, delegations |
| Main execution loop | `execution/runner.py` | LLM ↔ message routing ↔ code execution cycle |
| Memory management | `execution/MemoryManager.py` | Episodic compression to prevent context overflow |
| Action tracking | `execution/ActionSpace.py` | Serializes past/possible actions for LLM reasoning |
| Agent switching | `execution/agent_management.py` | Validates delegation commands, routes between agents |
| Artifact tracking | `execution/artifacts.py` | Collects code snippets, plots, data outputs from runs |
| Report generation | `execution/report_generation.py` | Generates per-agent reports, TODOs, NOTEs from conversations |
| Sandbox orchestration | `sandbox/` | Docker/Singularity container lifecycle + kernel API |
| LLM backends | `core/anthropic_wrapper.py`, `core/ollama_wrapper.py` | Unified interface for Claude, OpenAI, DeepSeek, Ollama |
| RAG system | `rag/RetrievalAugmentedGeneration.py` | Semantic knowledge base retrieval (SentenceTransformer embeddings) |
| Auto-metrics | `auto_metrics/` | Pluggable metric classes (QC, cell typing, batch correction, DEG, etc.) |
| Code samples | `code_samples/` | Reference bioinformatics patterns — not importable, used as agent context |
| Dataset access | `datasets/czi_datasets.py` | CELLxGENE Census browser and downloader |
| CLI | `cli/` | `caribou run`, `caribou create-system`, `caribou datasets`, `caribou config` |
| Tests | `caribou/tests/` | Unit + integration tests for the above |

### Key Architectural Patterns
- **Agent blueprints** — JSON configs define agent capabilities, commands, code samples, RAG enablement
- **Sandbox isolation** — all agent-generated code runs in Docker/Singularity containers
- **Episodic memory** — MemoryManager compresses history to prevent context overflow, with pinned early messages
- **Action space tracking** — LLM is given a serialized view of past and possible actions
- **Multi-LLM support** — unified OpenAI-compatible interface for Claude, GPT, DeepSeek, Ollama
- **Pluggable metrics** — auto_metrics registry allows lazy-loading of metric classes with complex dependencies

### The One Bridge
`auto_metrics/` is defined inside the agent package but *called* by the benchmarking harness (`execution/benchmark_runner.py`). This is the only intentional coupling point between the two systems.

---

## System 2: The Benchmarking Infrastructure (the evaluation harness)

**Root:** `benchmarking/` + `dev/` + `manuscript/`

Everything that tests and measures the agent. Not part of the product.

### Benchmark Suites

| Suite | Path | What it measures |
|---|---|---|
| Task benchmarks | `benchmarking/task_benchmarks/` | One-shot vs. single-agent vs. full-system on QC, data loading, doublets, batch correction |
| Cell-typing benchmarks | `benchmarking/celltyping_benchmarks/` | Annotation accuracy (ARI, NMI, F1) on ABA and TSP datasets |
| Integration benchmarks | `benchmarking/integration_benchmarks/` | SCIB-based batch correction quality on multi-batch data |
| Metadata benchmarks | `benchmarking/metadata_benchmarks/` | Metadata inference from anonymized datasets, scalability testing |

### Supporting Infrastructure

| Component | Path | Role |
|---|---|---|
| Evaluation scripts | `benchmarking/*/analysis/evaluate.py` | Compute metrics from raw run outputs |
| Results collection | `benchmarking/*/analysis/collect_results.py` | Aggregate across runs/modes/LLMs |
| SLURM job scripts | `benchmarking/*/slurm/` | HPC array job submission |
| Bash equivalents | `benchmarking/*/bash/` | Non-SLURM job submission |
| Log analysis toolkit | `dev/log_analysis_toolkit/` | Parse and visualize agent execution logs |
| Manuscript figures | `dev/manuscript_analysis/`, `manuscript/` | Publication-quality plot generation |
| Archived run outputs | `dev/auto_runs/`, `dev/partial_auto_runs/` | 120+ historical benchmark runs (chatgpt/deepseek/claude × modes × replicas) |
| Extra tools | `extra_tools/` | Interactive/one-shot agent testers, prompt evolver, LLM-based evaluator |

### Cell-Typing Benchmark Datasets
- `aba_hippocampus` — Mouse hippocampus (Allen Brain Atlas, ~86k cells); no cell_type column → only gene expression metrics
- `tsp_large_intestine` — Human large intestine (Tabula Sapiens, ~30k cells); fully configured with barcode join and coarse label mappings

### Deprecated / Superseded
- `dev/comparisons/` — superseded by `benchmarking/celltyping_benchmarks/` and `benchmarking/integration_benchmarks/`
- `dev/task_benchmarks/` — superseded by `benchmarking/task_benchmarks/`
- `dev/OLD_metadata_benchmarks/` — superseded by `benchmarking/metadata_benchmarks/`

---

## Key File Paths Quick Reference

### Agent System
```
caribou/src/caribou/agents/AgentSystem.py
caribou/src/caribou/execution/runner.py
caribou/src/caribou/execution/MemoryManager.py
caribou/src/caribou/execution/benchmark_runner.py
caribou/src/caribou/auto_metrics/registry.py
caribou/src/caribou/sandbox/benchmarking_sandbox_management.py
caribou/src/caribou/core/anthropic_wrapper.py
caribou/src/caribou/rag/RetrievalAugmentedGeneration.py
caribou/src/caribou/cli/main.py
```

### Benchmarking
```
benchmarking/task_benchmarks/src/one_shot_runner.py
benchmarking/celltyping_benchmarks/analysis/evaluate.py
benchmarking/celltyping_benchmarks/datasets/{id}/config.json
benchmarking/integration_benchmarks/src/evaluate.py
benchmarking/metadata_benchmarks/evaluate_metadata_results.py
```

### Analysis & Figures
```
dev/manuscript_analysis/generate_figure*.py
dev/log_analysis_toolkit/analyze_agent_logs.py
manuscript/compile.py
```
