# CARIBOU Frontend Architecture

## Layer Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  CLIENT MACHINE                                             │
│                                                             │
│  Browser                                                    │
│    └── Angular App (localhost:8000)                         │
│          ├── HTTP  → REST API (sessions, files, config)     │
│          └── WS    → WebSocket (streaming agent events)     │
└────────────────────────┬────────────────────────────────────┘
                         │  SSH tunnel
                         │  ssh -L 8000:localhost:8000 user@hpc
                         │
┌────────────────────────▼────────────────────────────────────┐
│  HPC NODE                                                   │
│                                                             │
│  caribou-server  (FastAPI, port 8000)                       │
│    ├── serves Angular dist/ (static files)                  │
│    ├── REST endpoints                                        │
│    └── WebSocket endpoint                                   │
│          │                                                  │
│          ▼                                                  │
│  runner.py  (AgentSystem execution engine)                  │
│    ├── MemoryManager                                        │
│    ├── AgentSystem (blueprint)                              │
│    └── LLM backend (Anthropic / OpenAI / Ollama)           │
│          │                                                  │
│          ▼                                                  │
│  sandbox  (Docker / Singularity container)                  │
│    └── kernel_api.py → Jupyter kernel                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Single port, single process
The server serves both the Angular static bundle AND the API on port 8000.
One SSH tunnel command, one process to manage on HPC.

### WebSocket for all agent interaction
All real-time agent communication (streaming tokens, code events, agent switches,
artifacts) flows through a single WebSocket connection per session. REST handles
everything that is not real-time.

### Server owns session lifecycle
The server is the only thing that talks to runner.py. Angular never imports or
calls CARIBOU Python directly — all interaction goes through the API.

### Stateful sessions on server
Each session is a running Python coroutine on the server side, wrapping a
runner.py instance. Sessions persist in memory (with disk serialization for
recovery) across WebSocket reconnects.

## Session Modes

Three modes mirror the existing CARIBOU execution modes:

| Mode | Description | User interaction |
|---|---|---|
| `interactive` | User sends messages, agent responds, back and forth | Active per turn |
| `auto` | User sends initial prompt, agent runs for N turns autonomously | Fire and monitor |
| `benchmark` | Structured run against a task with auto-metrics | Fire and collect results |

## Run Modes (from existing system)

| Mode | Description |
|---|---|
| `full_system` | Multi-agent CARIBOU with delegation |
| `single_agent` | Solo agent, no delegation |
| `one_shot` | Single LLM call, no agent framework |
