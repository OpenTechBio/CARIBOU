# CARIBOU Server API Surface

Full REST + WebSocket specification for `caribou/src/caribou/server/`.
Pseudocode schemas — not implementation code.

---

## Data Models

```
Session {
  id:             string (uuid)
  status:         enum { initializing | idle | running | stopped | error }
  mode:           enum { interactive | auto | benchmark }
  run_mode:       enum { full_system | single_agent | one_shot }
  agent_system:   string            # blueprint name, e.g. "olaf_v2"
  llm_backend:    string            # "claude-sonnet-4-5", "gpt-4o", "deepseek-r1"
  sandbox_type:   enum { singularity | docker | offline }
  dataset_path:   string            # absolute path on HPC to .h5ad file
  max_turns:      int | null        # null = unlimited (interactive mode)
  current_turn:   int
  current_agent:  string            # name of the active agent
  created_at:     datetime
  updated_at:     datetime
  artifacts:      Artifact[]
}

Message {
  id:             string (uuid)
  session_id:     string
  turn:           int
  role:           enum { user | assistant | system }
  agent_name:     string            # which agent produced this
  content:        string            # full text content
  timestamp:      datetime
  is_delegation:  bool              # true if this message triggered agent switch
}

Artifact {
  id:             string (uuid)
  session_id:     string
  turn:           int
  type:           enum { plot | data | code | report }
  filename:       string
  mime_type:      string            # "image/png", "text/csv", "application/x-hdf5"
  size_bytes:     int
  created_at:     datetime
  download_url:   string            # /api/sessions/{id}/artifacts/{artifact_id}/download
}

CodeEvent {
  turn:           int
  agent_name:     string
  source:         string            # the Python code string submitted
  stdout:         string
  stderr:         string
  result:         string | null     # return value if any
  success:        bool
  duration_ms:    int
}

AgentBlueprint {
  name:           string
  description:    string
  agents:         string[]          # list of agent names in the system
  has_rag:        bool
  path:           string            # absolute path to JSON file on HPC
}

LLMBackend {
  id:             string            # "claude-sonnet-4-5"
  provider:       enum { anthropic | openai | deepseek | ollama }
  display_name:   string
  available:      bool              # false if API key missing or model unreachable
}

Dataset {
  filename:       string
  path:           string
  size_bytes:     int
  uploaded_at:    datetime
}

ServerStatus {
  version:        string
  sandbox_type:   string            # detected available sandbox on this node
  active_sessions: int
}
```

---

## REST Endpoints

### Server

```
GET /api/status
  → ServerStatus
  # Health check, version, active session count, detected sandbox

GET /api/config/backends
  → LLMBackend[]
  # All known LLM backends; available=false if key missing

GET /api/config/blueprints
  → AgentBlueprint[]
  # All agent system blueprints found in CARIBOU_HOME/agent_systems/
```

---

### Sessions

```
POST /api/sessions
  body: {
    mode:          enum { interactive | auto | benchmark }
    run_mode:      enum { full_system | single_agent | one_shot }
    agent_system:  string           # blueprint name
    llm_backend:   string           # backend id
    sandbox_type:  enum { singularity | docker | offline }
    dataset_path:  string           # path to .h5ad on HPC
    max_turns:     int | null
    initial_prompt: string | null   # required for auto/benchmark mode
  }
  → Session
  # Creates session, initializes sandbox, loads agent system
  # Does NOT start execution yet — that begins when WS connects and sends run

GET /api/sessions
  → Session[]
  # All sessions (active + stopped), newest first

GET /api/sessions/{session_id}
  → Session

DELETE /api/sessions/{session_id}
  → { ok: true }
  # Terminates session if running, tears down sandbox, frees memory

GET /api/sessions/{session_id}/messages
  query: { offset?: int, limit?: int }
  → Message[]
  # Full conversation history for the session

GET /api/sessions/{session_id}/artifacts
  → Artifact[]

GET /api/sessions/{session_id}/artifacts/{artifact_id}/download
  → file (binary stream)
  # Serves the artifact file (plot PNG, .h5ad, CSV, etc.)

GET /api/sessions/{session_id}/code_events
  → CodeEvent[]
  # All code submission/execution events for the session
```

---

### Files / Datasets

```
POST /api/datasets/upload
  body: multipart/form-data { file: .h5ad }
  → Dataset
  # Saves to a managed uploads dir on HPC; returns path for use in session creation

GET /api/datasets
  → Dataset[]
  # Lists all uploaded datasets + any pre-existing datasets in known paths

DELETE /api/datasets/{filename}
  → { ok: true }
```

---

## WebSocket Protocol

### Endpoint

```
WS /ws/sessions/{session_id}
```

One WebSocket connection per active session. The connection carries all real-time
bidirectional communication for that session.

---

### Client → Server Messages

```
# Start execution (interactive: first turn; auto/benchmark: begin run)
{
  type: "run"
  content: string          # user message or initial prompt
}

# Send next turn (interactive mode only, after agent has gone idle)
{
  type: "user_message"
  content: string
}

# Stop current execution mid-run
{
  type: "stop"
}

# Ping (keepalive)
{
  type: "ping"
}
```

---

### Server → Client Events

All server events share an envelope:

```
{
  type:       string        # event type (see below)
  session_id: string
  turn:       int
  timestamp:  datetime
  data:       object        # event-specific payload
}
```

#### Token streaming

```
{ type: "token",  data: { agent_name: string, token: string } }
# Emitted for every streamed token from the LLM.
# Angular accumulates these into the current message bubble.
```

#### Message complete

```
{
  type: "message_complete"
  data: {
    message: Message        # full assembled message with id, role, content
  }
}
# Fired when the LLM finishes generating a full response.
# Angular should persist this to its local message list.
```

#### Agent delegation

```
{
  type: "agent_switch"
  data: {
    from_agent: string
    to_agent:   string
    command:    string      # the delegation command issued
    reason:     string | null
  }
}
# Fired when runner.py switches the active agent.
# Angular updates the active agent indicator in the UI.
```

#### Code submitted to sandbox

```
{
  type: "code_submitted"
  data: {
    agent_name: string
    source:     string      # the Python code block
  }
}
# Fired when runner.py sends code to the kernel.
# Angular shows a collapsible code block in the message stream.
```

#### Code execution result

```
{
  type: "code_result"
  data: {
    agent_name: string
    stdout:     string
    stderr:     string
    success:    bool
    duration_ms: int
  }
}
# Fired when the kernel returns a result.
# Angular appends stdout/stderr into the code block.
```

#### Artifact created

```
{
  type: "artifact"
  data: {
    artifact: Artifact      # full Artifact object including download_url
  }
}
# Fired when runner.py captures a plot, saved file, or report.
# Angular renders inline if plot/image, or shows download button otherwise.
```

#### Session status change

```
{
  type: "status_change"
  data: {
    status:  enum { idle | running | stopped | error }
    reason:  string | null
  }
}
# idle    → agent finished a turn, waiting for user (interactive mode)
# running → agent is actively executing
# stopped → max_turns reached or user stopped
# error   → unrecoverable error, session is dead
```

#### Auto-metrics result (benchmark mode)

```
{
  type: "metrics_result"
  data: {
    metric_name:  string
    value:        number | string | object
    passed:       bool | null
  }
}
```

#### Error

```
{
  type: "error"
  data: {
    code:    string         # e.g. "SANDBOX_TIMEOUT", "LLM_ERROR", "OOM"
    message: string
    fatal:   bool           # if true, session is dead
  }
}
```

#### Pong

```
{ type: "pong", data: {} }
```

---

## Event Stream Walkthrough

A typical interactive turn looks like this sequence of server → client events:

```
status_change   { status: "running" }
token           { token: "I" }
token           { token: " will" }
token           { token: " now" }
...             (many tokens)
code_submitted  { source: "import scanpy as sc\n..." }
code_result     { stdout: "...", success: true }
token           { token: "The" }
token           { token: " QC" }
...
artifact        { type: "plot", filename: "umap.png", download_url: "..." }
message_complete { message: { content: "...", agent_name: "QC_Agent" } }
status_change   { status: "idle" }
```

An agent delegation mid-turn:

```
token           { token: "I" }
...
agent_switch    { from_agent: "Orchestrator", to_agent: "QC_Agent", command: "run_qc" }
token           { token: "Running" }
...
```

---

## Error Handling Contract

- Non-fatal errors (e.g. code execution failure) emit `error { fatal: false }` and
  the session remains alive. Runner continues.
- Fatal errors (sandbox crash, OOM, unhandled exception in runner) emit
  `error { fatal: true }` followed by `status_change { status: "error" }`.
  The WebSocket connection is then closed by the server.
- The Angular app should handle WebSocket disconnection by attempting reconnect
  (up to N retries with backoff) and re-subscribing to the same session_id.
  The server must be able to reattach a new WS connection to an existing live session.
