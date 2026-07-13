# CARIBOU Test Suite - Quick Start Guide

## Installation

1. **Create or update one shared Conda control-plane environment:**

```bash
export CARIBOU_CONDA_PREFIX=/path/on/shared-software/caribou
export PYTHONNOUSERSITE=1
conda env update --prefix "$CARIBOU_CONDA_PREFIX" \
  --file caribou/environment.control-plane.yml
```

Do not create a `venv`, `.venv`, or per-run Conda environment in the repository
or experiment workspace. On HPC, keep this single prefix and the Conda package
cache on the designated software filesystem to avoid duplicating small files.

2. **Run through the shared prefix:**

```bash
CARIBOU_CONDA_PREFIX="$CARIBOU_CONDA_PREFIX" caribou/tests/run_tests.sh
```

Or invoke Python directly without shell activation:

```bash
conda run --no-capture-output --prefix "$CARIBOU_CONDA_PREFIX" \
  python -m pytest caribou/tests
```

The host Conda environment is for the CLI, web control plane, and tests. Actual
analyses remain inside versioned Docker/Apptainer images and run through Slurm;
Conda is not rebuilt inside each job.
The test runner sets `PYTHONNOUSERSITE=1` so packages cannot leak in from
`~/.local` and invalidate the recorded environment identity.

## Running Tests

### Option 1: Using the Test Runner Script (Recommended)

```bash
cd caribou/tests
./run_tests.sh
```

Available options:
- `./run_tests.sh --unit` - Run only unit tests
- `./run_tests.sh --integration` - Run only integration tests
- `./run_tests.sh --verbose` - Verbose output
- `./run_tests.sh --coverage` - Generate coverage report

### Option 2: Using pytest Directly

From the project root directory:

```bash
# Run all tests
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/

# Run specific test categories
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/unit/
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/integration/

# Run specific test file
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/unit/test_message_utils.py

# Run with verbose output
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/ -v

# Run with coverage
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest caribou/tests/ --cov=caribou --cov-report=html
```

## What Gets Tested

✅ **LLM API Wrappers**
- AnthropicClient (OpenAI compatibility)
- OllamaClient (local models)

✅ **Message Routing**
- Delegation detection (`delegate_to_agent`)
- RAG query detection (`query_rag_<topic>`)
- Artifact extraction (notes, TODOs)

✅ **History Management**
- MemoryManager with episodic summarization
- Context assembly and compression

✅ **Agent System**
- Multi-agent configuration
- Prompt generation
- Agent switching

✅ **End-to-End Integration**
- Complete message flows
- Multi-agent conversations
- Error handling

## Verifying the Setup

Run a quick smoke test:

```bash
conda run --prefix "$CARIBOU_CONDA_PREFIX" python -m pytest \
  caribou/tests/unit/test_message_utils.py::TestDelegationDetection::test_detect_simple_delegation -v
```

Expected output:
```
test_detect_simple_delegation PASSED
```

## Troubleshooting

**Import errors?**
The tests automatically add `caribou/src` to the Python path via `conftest.py`. If you still get import errors, verify the directory structure:

```
CARIBOU/
└── caribou/
    ├── src/
    │   └── caribou/
    │       ├── core/
    │       ├── execution/
    │       └── agents/
    └── tests/
        ├── conftest.py  # ← Should add src/ to path
        ├── unit/
        └── integration/
```

**Tests hanging?**
All external API calls are mocked - tests should run quickly (< 10 seconds total).

## Next Steps

- See [README.md](README.md) for detailed documentation
- Run with coverage to see what's tested: `./run_tests.sh --coverage`
- Open `htmlcov/index.html` to view the coverage report
