from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv, set_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from caribou.config import CARIBOU_HOME, DEFAULT_AGENT_DIR, ENV_FILE
from caribou.server.models import AgentBlueprint, LLMBackend, ServerStatus
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
        available = True if key_name is None else bool(os.environ.get(key_name))
        result.append(b.copy(update={"available": available}))
    return result


@router.get("/config/blueprints", response_model=List[AgentBlueprint])
async def get_blueprints() -> List[AgentBlueprint]:
    from caribou.agents.AgentSystem import AgentSystem
    from caribou.cli.run_cli import PACKAGE_AGENTS_DIR

    blueprints = []
    for search_dir in (DEFAULT_AGENT_DIR, PACKAGE_AGENTS_DIR):
        p = Path(search_dir)
        if not p.exists():
            continue
        for json_file in sorted(p.glob("*.json")):
            try:
                sys = AgentSystem.load_from_json(str(json_file))
                blueprints.append(AgentBlueprint(
                    name=json_file.stem,
                    description=getattr(sys, "description", json_file.stem),
                    agents=list(sys.agents.keys()),
                    has_rag=any(a.is_rag_enabled for a in sys.agents.values()),
                    path=str(json_file),
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


class UpdateSettingsRequest(BaseModel):
    sessions_dir: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None


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


def _detect_sandbox() -> str:
    import shutil
    if shutil.which("singularity"):
        return "singularity"
    if shutil.which("docker"):
        return "docker"
    return "offline"
