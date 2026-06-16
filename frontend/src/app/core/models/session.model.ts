export type SessionStatus = 'initializing' | 'idle' | 'running' | 'stopped' | 'error';
export type SessionMode = 'interactive' | 'auto';
export type RunMode = 'full_system' | 'single_agent' | 'one_shot';
export type SandboxType = 'singularity' | 'docker' | 'offline';
export type ArtifactType = 'plot' | 'data' | 'code' | 'report';

export interface Session {
  id: string;
  status: SessionStatus;
  mode: SessionMode;
  run_mode: RunMode;
  agent_system: string;
  llm_backend: string;
  sandbox_type: SandboxType;
  dataset_path: string;
  max_turns: number | null;
  current_turn: number;
  current_agent: string;
  created_at: string;
  updated_at: string;
  artifact_count: number;
  message_count: number;
}

export interface SessionCreateRequest {
  mode: SessionMode;
  run_mode: RunMode;
  agent_system: string;
  llm_backend: string;
  ollama_model?: string;
  sandbox_type: SandboxType;
  dataset_path: string;
  reference_dataset_path?: string;
  max_turns?: number;
  initial_prompt?: string;
  compress_memory?: boolean;
  agent_report_memory?: boolean;
}

export interface Message {
  id: string;
  session_id: string;
  turn: number;
  role: string;
  agent_name: string;
  content: string;
  timestamp: string;
  is_delegation: boolean;
}

export interface Artifact {
  id: string;
  session_id: string;
  turn: number;
  type: ArtifactType;
  filename: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
  local_path: string;
  download_url: string;
}

export interface CodeEvent {
  id: string;
  session_id: string;
  turn: number;
  agent_name: string;
  source: string;
  stdout: string;
  stderr: string;
  success: boolean;
  duration_ms: number;
}

export interface LLMBackend {
  id: string;
  provider: string;
  display_name: string;
  available: boolean;
  status?: string | null;
  message?: string | null;
  suggested_fix?: string | null;
}

export interface OllamaModelsResponse {
  host: string;
  running: boolean;
  models: string[];
  default_model: string;
  status: string;
  message: string;
  suggested_fix?: string | null;
}

export interface AgentBlueprint {
  name: string;
  description: string;
  agents: string[];
  has_rag: boolean;
  path: string;
  is_package_default: boolean;
}

export interface ServerStatus {
  version: string;
  sandbox_type: string;
  active_sessions: number;
}

export interface Dataset {
  filename: string;
  path: string;
  size_bytes: number;
  uploaded_at: string;
}
