from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SessionStatus(str, Enum):
    initializing = "initializing"
    idle = "idle"
    running = "running"
    stopped = "stopped"
    error = "error"


class SessionMode(str, Enum):
    interactive = "interactive"
    auto = "auto"


class RunMode(str, Enum):
    full_system = "full_system"
    single_agent = "single_agent"
    one_shot = "one_shot"


class SandboxType(str, Enum):
    singularity = "singularity"
    docker = "docker"
    offline = "offline"


class ArtifactType(str, Enum):
    plot = "plot"
    data = "data"
    code = "code"
    report = "report"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class MemoryStrategy(str, Enum):
    full = "full"
    episodic = "episodic"
    agent_report = "agent_report"
    none = "none"


class MemoryConfigResponse(BaseModel):
    strategy: str = "full"
    working_history_size: Optional[int] = None
    summarization_threshold: Optional[int] = None
    chunk_size: Optional[int] = None


class SessionCreateRequest(BaseModel):
    mode: SessionMode
    run_mode: RunMode = RunMode.full_system
    agent_system: str
    llm_backend: str
    model_name: Optional[str] = None
    ollama_model: Optional[str] = None
    sandbox_type: SandboxType = SandboxType.singularity
    dataset_path: str
    reference_dataset_path: Optional[str] = None
    max_turns: Optional[int] = None
    initial_prompt: Optional[str] = None
    memory_strategy: MemoryStrategy = MemoryStrategy.full
    memory_working_history_size: Optional[int] = None
    memory_summarization_threshold: Optional[int] = None
    memory_chunk_size: Optional[int] = None
    compress_memory: bool = False
    agent_report_memory: bool = False


class ResolvedModelInfo(BaseModel):
    """Exact model identity and effective request controls for provenance."""

    provider: str
    model: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


class MessageRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    turn: int
    role: str
    agent_name: str
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    is_delegation: bool = False


class ArtifactRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    turn: int
    type: ArtifactType
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
    local_path: str = ""

    @property
    def download_url(self) -> str:
        return f"/api/sessions/{self.session_id}/artifacts/{self.id}/download"


class CodeEventRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    turn: int
    agent_name: str
    source: str
    stdout: str = ""
    stderr: str = ""
    success: bool = True
    duration_ms: int = 0


class EvaluationResult(BaseModel):
    session_id: str
    turn: int
    evaluator_agent: str
    evaluator_source: str
    model: str
    assessment: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SessionResponse(BaseModel):
    id: str
    status: SessionStatus
    mode: SessionMode
    run_mode: RunMode
    agent_system: str
    llm_backend: str
    resolved_model: Optional[ResolvedModelInfo] = None
    sandbox_type: SandboxType
    dataset_path: str
    max_turns: Optional[int]
    current_turn: int
    current_agent: str
    created_at: datetime
    updated_at: datetime
    artifact_count: int
    message_count: int
    memory: Optional[MemoryConfigResponse] = None
    # False after a server restart until the runner is relaunched — restored
    # sessions don't carry a live llm_client/agent_system (see
    # session_persistence.py), so /evaluate would 400 even though current_turn > 0.
    can_evaluate: bool = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class LLMBackend(BaseModel):
    id: str
    provider: str
    display_name: str
    available: bool
    model_name: Optional[str] = None
    thinking: Optional[bool] = None
    status: Optional[str] = None
    message: Optional[str] = None
    suggested_fix: Optional[str] = None


class AgentBlueprint(BaseModel):
    name: str
    description: str
    agents: List[str]
    has_rag: bool
    path: str
    is_package_default: bool = False


# ---------------------------------------------------------------------------
# Blueprint editor
# ---------------------------------------------------------------------------


class CommandConfig(BaseModel):
    target_agent: str
    description: str


class AgentConfig(BaseModel):
    prompt: str
    rag_enabled: bool = False
    neighbors: Dict[str, CommandConfig] = {}
    code_samples: List[str] = []


class BlueprintContent(BaseModel):
    name: str
    global_policy: str
    agents: Dict[str, AgentConfig]
    is_package_default: bool
    evaluator_agent: Optional[str] = None


class SaveBlueprintRequest(BaseModel):
    name: str
    global_policy: str
    agents: Dict[str, AgentConfig]
    evaluator_agent: Optional[str] = None


class ServerStatus(BaseModel):
    version: str = "0.1.0"
    sandbox_type: str
    active_sessions: int


class OllamaModelsResponse(BaseModel):
    host: str
    running: bool
    models: List[str]
    default_model: str
    status: str
    message: str
    suggested_fix: Optional[str] = None


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


class DatasetRecord(BaseModel):
    filename: str
    path: str
    size_bytes: int
    uploaded_at: datetime


class DatasetPathValidationRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# WebSocket messages (client → server)
# ---------------------------------------------------------------------------


class WSRunMessage(BaseModel):
    type: str = "run"
    content: str


class WSUserMessage(BaseModel):
    type: str = "user_message"
    content: str


class WSStopMessage(BaseModel):
    type: str = "stop"


# ---------------------------------------------------------------------------
# WebSocket events (server → client)  — raw dicts emitted by streaming_runner
# ---------------------------------------------------------------------------
# Shape: { type: str, session_id: str, turn: int, timestamp: str, data: dict }
# Types: token | message_complete | agent_switch | code_submitted |
#        code_result | artifact | status_change | metrics_result | error | pong


def make_event(
    event_type: str,
    data: Dict[str, Any],
    session_id: str = "",
    turn: int = 0,
) -> Dict[str, Any]:
    return {
        "type": event_type,
        "session_id": session_id,
        "turn": turn,
        "timestamp": datetime.utcnow().isoformat(),
        "data": data,
    }
