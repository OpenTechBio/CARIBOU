# Angular App Structure

Pseudocode-level sketch of the Angular project — services, components, and data flow.
Not implementation code — intent and responsibility mapping.

---

## Project Layout

```
frontend/
  src/
    app/
      core/                        # singleton services, interceptors, guards
        services/
          session.service.ts       # session CRUD, active session state
          agent-stream.service.ts  # WebSocket connection, event Observable
          dataset.service.ts       # file upload, dataset listing
          config.service.ts        # blueprints, LLM backends, server status
        models/
          session.model.ts         # TypeScript interfaces mirroring server models
          events.model.ts          # AgentEvent union type
        interceptors/
          error.interceptor.ts     # global HTTP error handling
        guards/
          session-active.guard.ts  # route guard: session must exist

      features/
        dashboard/                 # session list, create new session
        session/                   # main session view (the "cockpit")
          chat/                    # message stream panel
          sidebar/                 # artifact list, code events, session info
          controls/                # input bar, stop button, turn counter
        datasets/                  # file upload and dataset browser
        settings/                  # LLM backend config, API keys

      shared/
        components/
          message-bubble/          # renders a single agent message
          code-block/              # collapsible code + stdout/stderr
          artifact-card/           # plot preview or file download
          agent-badge/             # shows current active agent name
          status-indicator/        # session status chip
        pipes/
          relative-time.pipe.ts
          file-size.pipe.ts

    environments/
      environment.ts               # apiBase: 'http://localhost:8000'
      environment.prod.ts          # same — always localhost since SSH tunnel
```

---

## Core Services

### `SessionService`

```
responsibilities:
  - CRUD for sessions via REST
  - maintains activeSessions: Signal<Session[]>
  - maintains currentSession: Signal<Session | null>

key methods:
  createSession(config: SessionConfig): Observable<Session>
  getSessions(): Observable<Session[]>
  getSession(id: string): Observable<Session>
  deleteSession(id: string): Observable<void>
  getMessages(id: string): Observable<Message[]>
  getArtifacts(id: string): Observable<Artifact[]>
  getCodeEvents(id: string): Observable<CodeEvent[]>
```

### `AgentStreamService`

```
responsibilities:
  - owns the WebSocket connection for the active session
  - exposes a single Observable<AgentEvent> that the session view subscribes to
  - handles reconnection with exponential backoff
  - multiplexes typed event streams (tokens, artifacts, status, etc.)
  - manages keepalive pings

key methods:
  connect(sessionId: string): void
  disconnect(): void
  send(message: ClientMessage): void

key observables (derived from the raw event stream):
  events$:         Observable<AgentEvent>      # raw union stream
  tokens$:         Observable<TokenEvent>      # LLM token stream
  agentSwitch$:    Observable<AgentSwitchEvent>
  codeEvents$:     Observable<CodeEvent>       # submitted + result pairs
  artifacts$:      Observable<ArtifactEvent>
  statusChanges$:  Observable<StatusChangeEvent>
  errors$:         Observable<ErrorEvent>

reconnect strategy:
  on disconnect:
    if session.status != 'stopped' and session.status != 'error':
      retry up to 5 times with backoff (1s, 2s, 4s, 8s, 16s)
      on reconnect: re-subscribe, server reattaches to live session
      on all retries exhausted: emit synthetic error event, mark session dead
```

### `DatasetService`

```
responsibilities:
  - multipart file upload with progress tracking
  - dataset listing

key methods:
  uploadDataset(file: File): Observable<{ progress: number, dataset?: Dataset }>
  getDatasets(): Observable<Dataset[]>
  deleteDataset(filename: string): Observable<void>
```

### `ConfigService`

```
responsibilities:
  - fetches and caches server status, blueprints, LLM backends
  - loaded once on app init via APP_INITIALIZER

key observables:
  serverStatus$:  Observable<ServerStatus>
  blueprints$:    Observable<AgentBlueprint[]>
  backends$:      Observable<LLMBackend[]>     # only available=true backends shown
```

---

## Feature: Session View (the "cockpit")

This is the main UI — what the user sees while a session is running.

```
┌─────────────────────────────────────────────────────────────┐
│  CARIBOU  [session name]          [agent badge]  [status]   │
├───────────────────────────────┬─────────────────────────────┤
│                               │                             │
│   CHAT PANEL                  │   SIDEBAR                   │
│                               │                             │
│   [system message]            │   ┌─ Artifacts ───────────┐ │
│                               │   │  plot.png  [preview]  │ │
│   [user message]              │   │  data.csv  [download] │ │
│                               │   └───────────────────────┘ │
│   [agent message bubble]      │                             │
│     ▶ [code block]            │   ┌─ Code Events ─────────┐ │
│       stdout output           │   │  turn 3: 12 lines     │ │
│     [plot inline]             │   │  turn 5: 8 lines      │ │
│                               │   └───────────────────────┘ │
│   [streaming bubble...]       │                             │
│                               │   ┌─ Session Info ────────┐ │
│                               │   │  turns: 4 / 20        │ │
│                               │   │  LLM: claude-sonnet   │ │
│                               │   │  blueprint: olaf_v2   │ │
│                               │   └───────────────────────┘ │
├───────────────────────────────┴─────────────────────────────┤
│  [text input]                          [Stop]  [Send →]     │
└─────────────────────────────────────────────────────────────┘
```

### ChatComponent

```
responsibilities:
  - subscribes to tokens$ and assembles streaming message in place
  - on message_complete: replaces streaming bubble with finalized Message
  - on agent_switch: inserts a delegation indicator between messages
  - on code_submitted + code_result: injects CodeBlockComponent inline
  - on artifact (type=plot): injects inline image via artifact download_url
  - auto-scrolls to bottom on new content

state:
  messages:         Message[]        # confirmed complete messages
  streamingContent: string           # accumulated tokens for in-flight message
  streamingAgent:   string           # agent name for in-flight message
```

### MessageBubbleComponent

```
inputs:
  message:    Message
  streaming:  boolean     # if true, shows cursor, content is live-updating

renders:
  - agent name badge + timestamp
  - markdown-rendered content (code fences, bold, etc.)
  - embedded CodeBlockComponent if message has associated code events
  - embedded ArtifactCard if message has associated plot artifacts
```

### CodeBlockComponent

```
inputs:
  source:     string      # Python code submitted
  stdout:     string
  stderr:     string
  success:    boolean
  duration_ms: number

renders:
  - collapsible panel, collapsed by default
  - syntax-highlighted Python source
  - stdout/stderr tabs
  - execution time + pass/fail indicator
```

### ArtifactCardComponent

```
inputs:
  artifact:   Artifact

renders:
  - if type=plot: inline <img> loading from download_url
  - if type=data: filename + size + download button
  - if type=report: expandable text panel
```

### ControlsComponent

```
responsibilities:
  - text input (disabled while status=running in auto mode)
  - send button (calls agentStream.send({ type: 'user_message', content }))
  - stop button (calls agentStream.send({ type: 'stop' }), visible while running)
  - turn counter display
```

---

## Feature: Dashboard (session list + create)

```
responsibilities:
  - lists all sessions with status chips
  - "New Session" button opens creation dialog

CreateSessionDialog:
  step 1: select blueprint (blueprint dropdown)
  step 2: select dataset (dataset browser + upload)
  step 3: configure run (LLM backend, mode, max_turns, initial_prompt for auto)
  step 4: confirm + create → navigate to /session/{id}
```

---

## Routing

```
/                        → redirect to /dashboard
/dashboard               → DashboardComponent
/session/:id             → SessionComponent  (guard: session must exist)
/datasets                → DatasetsComponent
/settings                → SettingsComponent
```

---

## State Management Approach

Use Angular Signals for local component state and service-level reactive state.
Use RxJS Observables for the WebSocket stream (inherently async/streaming).
No NgRx needed at this scale — service-level Signals + the stream Observable
is sufficient. Revisit if session history or cross-session state grows complex.

---

## Key TypeScript Types

```typescript
// Agent event union — exhaustive switch in components
type AgentEvent =
  | { type: 'token';            data: TokenEventData }
  | { type: 'message_complete'; data: MessageCompleteData }
  | { type: 'agent_switch';     data: AgentSwitchData }
  | { type: 'code_submitted';   data: CodeSubmittedData }
  | { type: 'code_result';      data: CodeResultData }
  | { type: 'artifact';         data: ArtifactData }
  | { type: 'status_change';    data: StatusChangeData }
  | { type: 'metrics_result';   data: MetricsResultData }
  | { type: 'error';            data: ErrorData }
  | { type: 'pong';             data: {} }

// Session creation config
interface SessionConfig {
  mode:           'interactive' | 'auto' | 'benchmark'
  run_mode:       'full_system' | 'single_agent' | 'one_shot'
  agent_system:   string
  llm_backend:    string
  sandbox_type:   'singularity' | 'docker' | 'offline'
  dataset_path:   string
  max_turns:      number | null
  initial_prompt: string | null
}
```
