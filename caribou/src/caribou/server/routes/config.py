from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv, set_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import shutil

from caribou.config import CARIBOU_HOME, DEFAULT_AGENT_DIR, ENV_FILE
from caribou.server.models import (
    AgentBlueprint, AgentConfig, BlueprintContent, CommandConfig,
    LLMBackend, OllamaModelsResponse, SaveBlueprintRequest, ServerStatus,
)
from caribou.server.ollama_service import (
    DEFAULT_OLLAMA_MODEL,
    OllamaStartupError,
    normalize_host,
    probe_ollama,
    start_ollama,
)
from caribou.server.session_manager import session_manager, _SESSIONS_DIR

router = APIRouter(prefix="/api", tags=["config"])

_BACKENDS = [
    LLMBackend(id="chatgpt", provider="openai", display_name="GPT-4o (OpenAI)", available=False),
    LLMBackend(id="claude", provider="anthropic", display_name="Claude Sonnet (Anthropic)", available=False),
    LLMBackend(id="deepseek", provider="deepseek", display_name="DeepSeek Chat", available=False),
    LLMBackend(id="ollama", provider="ollama", display_name="Ollama (local)", available=False),
]

_KEY_MAP = {
    "chatgpt": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "ollama": None,
}


@router.get("/status", response_model=ServerStatus)
async def get_status() -> ServerStatus:
    return ServerStatus(
        sandbox_type=_detect_sandbox(),
        active_sessions=len([
            s for s in session_manager.list_sessions()
            if s.status.value in ("initializing", "idle", "running")
        ]),
    )


@router.get("/config/backends", response_model=List[LLMBackend])
async def get_backends() -> List[LLMBackend]:
    load_dotenv(dotenv_path=ENV_FILE)
    result = []
    for b in _BACKENDS:
        key_name = _KEY_MAP.get(b.id)
        if b.id == "ollama":
            ollama = probe_ollama(os.environ.get("OLLAMA_HOST"))
            available = ollama.status in {"ready", "not_running"}
            result.append(b.copy(update={
                "available": available,
                "status": ollama.status,
                "message": ollama.message,
                "suggested_fix": ollama.suggested_fix,
            }))
        else:
            available = bool(os.environ.get(key_name)) if key_name else True
            result.append(b.copy(update={"available": available}))
    return result


@router.get("/config/blueprints", response_model=List[AgentBlueprint])
async def get_blueprints() -> List[AgentBlueprint]:
    from caribou.agents.AgentSystem import AgentSystem
    from caribou.cli.run_cli import PACKAGE_AGENTS_DIR

    blueprints = []
    seen_names: set = set()
    for search_dir, is_pkg in ((DEFAULT_AGENT_DIR, False), (Path(PACKAGE_AGENTS_DIR), True)):
        p = Path(search_dir)
        if not p.exists():
            continue
        for json_file in sorted(p.glob("*.json")):
            if json_file.stem in seen_names:
                continue
            seen_names.add(json_file.stem)
            try:
                sys = AgentSystem.load_from_json(str(json_file))
                blueprints.append(AgentBlueprint(
                    name=json_file.stem,
                    description=getattr(sys, "description", json_file.stem),
                    agents=list(sys.agents.keys()),
                    has_rag=any(a.is_rag_enabled for a in sys.agents.values()),
                    path=str(json_file),
                    is_package_default=is_pkg,
                ))
            except Exception:
                pass
    return blueprints


class ServerSettings(BaseModel):
    caribou_home: str
    sessions_dir: str
    uploads_dir: str
    env_file: str
    api_keys: Dict[str, str]  # key name → masked value
    ollama_host: str
    ollama_model: str


class UpdateSettingsRequest(BaseModel):
    sessions_dir: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    ollama_host: Optional[str] = None
    ollama_model: Optional[str] = None


@router.get("/settings", response_model=ServerSettings)
async def get_settings() -> ServerSettings:
    load_dotenv(dotenv_path=ENV_FILE, override=True)

    def _mask(val: str) -> str:
        if not val:
            return ""
        return val[:8] + "•" * min(len(val) - 8, 32) if len(val) > 8 else "•" * len(val)

    return ServerSettings(
        caribou_home=str(CARIBOU_HOME),
        sessions_dir=str(_SESSIONS_DIR),
        uploads_dir=str(CARIBOU_HOME / "server_uploads"),
        env_file=str(ENV_FILE),
        api_keys={
            "OPENAI_API_KEY":    _mask(os.environ.get("OPENAI_API_KEY", "")),
            "ANTHROPIC_API_KEY": _mask(os.environ.get("ANTHROPIC_API_KEY", "")),
            "DEEPSEEK_API_KEY":  _mask(os.environ.get("DEEPSEEK_API_KEY", "")),
        },
        ollama_host=normalize_host(os.environ.get("OLLAMA_HOST")),
        ollama_model=os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    )


@router.patch("/settings")
async def update_settings(body: UpdateSettingsRequest) -> dict:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.touch()

    updated = []
    if body.openai_api_key is not None:
        set_key(str(ENV_FILE), "OPENAI_API_KEY", body.openai_api_key)
        updated.append("OPENAI_API_KEY")
    if body.anthropic_api_key is not None:
        set_key(str(ENV_FILE), "ANTHROPIC_API_KEY", body.anthropic_api_key)
        updated.append("ANTHROPIC_API_KEY")
    if body.deepseek_api_key is not None:
        set_key(str(ENV_FILE), "DEEPSEEK_API_KEY", body.deepseek_api_key)
        updated.append("DEEPSEEK_API_KEY")
    if body.ollama_host is not None:
        set_key(str(ENV_FILE), "OLLAMA_HOST", normalize_host(body.ollama_host))
        updated.append("OLLAMA_HOST")
    if body.ollama_model is not None:
        set_key(str(ENV_FILE), "OLLAMA_MODEL", body.ollama_model.strip())
        updated.append("OLLAMA_MODEL")

    if body.sessions_dir is not None:
        p = Path(body.sessions_dir).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(400, f"Cannot create sessions directory: {exc}")
        set_key(str(ENV_FILE), "CARIBOU_SESSIONS_DIR", str(p))
        updated.append("CARIBOU_SESSIONS_DIR")

    load_dotenv(dotenv_path=ENV_FILE, override=True)
    return {"updated": updated}


@router.get("/config/ollama/models", response_model=OllamaModelsResponse)
async def get_ollama_models() -> OllamaModelsResponse:
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    default_model = os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    status = probe_ollama(os.environ.get("OLLAMA_HOST"))
    if default_model not in status.models and status.models:
        default_model = status.models[0]
    return OllamaModelsResponse(
        host=status.host,
        running=status.running,
        models=status.models,
        default_model=default_model,
        status=status.status,
        message=status.message,
        suggested_fix=status.suggested_fix,
    )


@router.post("/config/ollama/start", response_model=OllamaModelsResponse)
async def start_ollama_server() -> OllamaModelsResponse:
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    default_model = os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    try:
        status = start_ollama(os.environ.get("OLLAMA_HOST"))
    except OllamaStartupError as exc:
        raise HTTPException(
            400,
            {
                "code": exc.code,
                "message": str(exc),
                "suggested_fix": exc.suggested_fix,
            },
        )
    if default_model not in status.models and status.models:
        default_model = status.models[0]
    return OllamaModelsResponse(
        host=status.host,
        running=status.running,
        models=status.models,
        default_model=default_model,
        status=status.status,
        message=status.message,
        suggested_fix=status.suggested_fix,
    )


# ---------------------------------------------------------------------------
# Blueprint CRUD helpers
# ---------------------------------------------------------------------------

def _get_package_agents_dir() -> Path:
    from caribou.cli.run_cli import PACKAGE_AGENTS_DIR
    return Path(PACKAGE_AGENTS_DIR)


def _is_package_default(name: str) -> bool:
    return (_get_package_agents_dir() / f"{name}.json").exists()


def _resolve_blueprint_path(name: str) -> Path:
    """Return the path to a blueprint file, searching user dir then package dir."""
    for search_dir in (DEFAULT_AGENT_DIR, _get_package_agents_dir()):
        candidate = Path(search_dir) / f"{name}.json"
        if candidate.exists():
            return candidate
    raise HTTPException(404, f"Blueprint '{name}' not found.")


def _load_blueprint_content(name: str) -> BlueprintContent:
    """Parse a blueprint JSON file into BlueprintContent."""
    path = _resolve_blueprint_path(name)
    with open(path) as f:
        raw = json.load(f)

    agents: Dict[str, AgentConfig] = {}
    for agent_name, agent_raw in raw.get("agents", {}).items():
        neighbors = {
            cmd_name: CommandConfig(
                target_agent=cmd_data["target_agent"],
                description=cmd_data.get("description", ""),
            )
            for cmd_name, cmd_data in agent_raw.get("neighbors", {}).items()
        }
        agents[agent_name] = AgentConfig(
            prompt=agent_raw.get("prompt", ""),
            rag_enabled=agent_raw.get("rag", {}).get("enabled", False),
            neighbors=neighbors,
            code_samples=agent_raw.get("code_samples", []),
        )

    global_policy = raw.get("global_policy", "")
    if not isinstance(global_policy, str):
        global_policy = json.dumps(global_policy, indent=2)

    return BlueprintContent(
        name=name,
        global_policy=global_policy,
        agents=agents,
        is_package_default=_is_package_default(name),
    )


def _to_disk_dict(req: SaveBlueprintRequest) -> dict:
    """Convert SaveBlueprintRequest to the on-disk JSON structure."""
    agents_dict = {}
    for agent_name, agent in req.agents.items():
        agents_dict[agent_name] = {
            "prompt": agent.prompt,
            "rag": {"enabled": agent.rag_enabled},
            "neighbors": {
                cmd_name: {"target_agent": cmd.target_agent, "description": cmd.description}
                for cmd_name, cmd in agent.neighbors.items()
            },
            **({"code_samples": agent.code_samples} if agent.code_samples else {}),
        }
    return {"global_policy": req.global_policy, "agents": agents_dict}


def _validate_blueprint(req: SaveBlueprintRequest) -> None:
    """Raise HTTPException 422 on structural validation failure."""
    if not req.name or "/" in req.name or "\\" in req.name or req.name.endswith(".json"):
        raise HTTPException(422, "Invalid blueprint name.")
    if not req.agents:
        raise HTTPException(422, "Blueprint must have at least one agent.")
    agent_keys = set(req.agents.keys())
    for agent_name, agent in req.agents.items():
        if not agent_name:
            raise HTTPException(422, "Agent names must be non-empty.")
        for cmd_name, cmd in agent.neighbors.items():
            if cmd.target_agent not in agent_keys:
                raise HTTPException(
                    422,
                    f"Agent '{agent_name}' command '{cmd_name}' references unknown agent '{cmd.target_agent}'.",
                )


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Blueprint CRUD endpoints
# ---------------------------------------------------------------------------

@router.get("/config/blueprints/{name}", response_model=BlueprintContent)
async def get_blueprint(name: str) -> BlueprintContent:
    return _load_blueprint_content(name)


@router.post("/config/blueprints", response_model=BlueprintContent, status_code=201)
async def create_blueprint(req: SaveBlueprintRequest) -> BlueprintContent:
    _validate_blueprint(req)
    dest = DEFAULT_AGENT_DIR / f"{req.name}.json"
    if dest.exists():
        raise HTTPException(409, f"Blueprint '{req.name}' already exists.")
    _atomic_write(dest, _to_disk_dict(req))
    return _load_blueprint_content(req.name)


@router.put("/config/blueprints/{name}", response_model=BlueprintContent)
async def update_blueprint(name: str, req: SaveBlueprintRequest) -> BlueprintContent:
    if _is_package_default(name):
        raise HTTPException(403, f"Blueprint '{name}' is a package default and cannot be modified.")
    user_path = DEFAULT_AGENT_DIR / f"{name}.json"
    if not user_path.exists():
        raise HTTPException(404, f"Blueprint '{name}' not found in user blueprints.")
    _validate_blueprint(req)
    req = SaveBlueprintRequest(name=name, global_policy=req.global_policy, agents=req.agents)
    _atomic_write(user_path, _to_disk_dict(req))
    return _load_blueprint_content(name)


@router.delete("/config/blueprints/{name}", status_code=204)
async def delete_blueprint(name: str) -> None:
    if _is_package_default(name):
        raise HTTPException(403, f"Blueprint '{name}' is a package default and cannot be deleted.")
    user_path = DEFAULT_AGENT_DIR / f"{name}.json"
    if not user_path.exists():
        raise HTTPException(404, f"Blueprint '{name}' not found in user blueprints.")
    user_path.unlink()


_USER_CODE_SAMPLES_DIR = CARIBOU_HOME / "code_samples"


class ImportCodeSampleRequest(BaseModel):
    source_path: str


class ImportCodeSampleResponse(BaseModel):
    filename: str
    destination: str


@router.post("/config/code-samples/import", response_model=ImportCodeSampleResponse)
async def import_code_sample(req: ImportCodeSampleRequest) -> ImportCodeSampleResponse:
    src = Path(req.source_path)
    if not src.is_absolute():
        raise HTTPException(400, "source_path must be an absolute path.")
    if not src.exists():
        raise HTTPException(404, f"File not found: {src}")
    if not src.is_file():
        raise HTTPException(400, f"Path is not a regular file: {src}")

    _USER_CODE_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    dest = _USER_CODE_SAMPLES_DIR / src.name
    if dest.exists():
        raise HTTPException(409, f"A code sample named '{src.name}' already exists. Rename the source file or remove the existing sample.")

    shutil.copy2(src, dest)
    return ImportCodeSampleResponse(filename=src.name, destination=str(dest))


def _detect_sandbox() -> str:
    if shutil.which("singularity"):
        return "singularity"
    if shutil.which("docker"):
        return "docker"
    return "offline"
