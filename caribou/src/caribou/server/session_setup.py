"""
Session bootstrap helpers.

Isolated from `session_manager` so blueprint resolution and sandbox/LLM
construction can be reused (or tested) without touching the manager.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from caribou.config import DEFAULT_AGENT_DIR
from caribou.server.models import SessionCreateRequest
from caribou.server.session_state import (
    SANDBOX_DATA_PATH,
    SANDBOX_REF_DATA_PATH,
)


def find_blueprint(name: str) -> Path:
    """Resolve a blueprint name to a JSON path."""
    from caribou.cli.run_cli import PACKAGE_AGENTS_DIR

    for search_dir in (DEFAULT_AGENT_DIR, PACKAGE_AGENTS_DIR):
        candidate = Path(search_dir) / name
        if candidate.exists():
            return candidate
        candidate = Path(search_dir) / f"{name}.json"
        if candidate.exists():
            return candidate

    # Absolute path provided
    p = Path(name)
    if p.exists():
        return p

    raise FileNotFoundError(
        f"Blueprint '{name}' not found in {DEFAULT_AGENT_DIR} or package agents dir."
    )


def build_llm_client(config: SessionCreateRequest):
    """Return (llm_client, model_name) for the given backend string."""
    from openai import OpenAI

    backend = config.llm_backend

    if backend == "chatgpt":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise EnvironmentError("OPENAI_API_KEY not set.")
        return OpenAI(api_key=key), "gpt-4o"

    if backend == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set.")
        from caribou.core.anthropic_wrapper import AnthropicClient
        return AnthropicClient(api_key=key), "claude-sonnet-4-6"

    if backend == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise EnvironmentError("DEEPSEEK_API_KEY not set.")
        return OpenAI(api_key=key, base_url="https://api.deepseek.com"), "deepseek-chat"

    if backend.startswith("ollama"):
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        env_model = os.environ.get("OLLAMA_MODEL", "")
        requested_model = config.ollama_model or env_model or ""
        from caribou.server.ollama_service import ensure_ollama_ready
        from caribou.core.ollama_wrapper import OllamaClient
        resolved_host, model_name = ensure_ollama_ready(host, requested_model)
        return OllamaClient(host=resolved_host, model=model_name), model_name

    raise ValueError(f"Unknown LLM backend: {backend!r}")


def build_sandbox(config: SessionCreateRequest, output_dir: Path):
    """Build and start a sandbox manager. Blocking — run in a thread."""
    from rich.console import Console
    from caribou.core.sandbox_management import init_docker, init_singularity_exec

    script_dir = Path(__file__).resolve().parent
    # Quiet console so sandbox init output doesn't go to stdout;
    # errors are surfaced via exceptions caught by the caller.
    console = Console(quiet=True)

    if config.sandbox_type.value == "docker":
        manager_class, handle, copy_cmd, _, _ = init_docker(
            script_dir, subprocess, console, force_refresh=False
        )
        sandbox = manager_class()
        if not sandbox.start_container():
            raise RuntimeError("Docker sandbox failed to start.")
        copy_cmd(config.dataset_path, f"{handle}:{SANDBOX_DATA_PATH}")
        if config.reference_dataset_path:
            copy_cmd(config.reference_dataset_path, f"{handle}:{SANDBOX_REF_DATA_PATH}")
        return sandbox

    if config.sandbox_type.value == "singularity":
        manager_class, _, _, _, _ = init_singularity_exec(
            script_dir, SANDBOX_DATA_PATH, subprocess, console, force_refresh=False
        )
        sandbox = manager_class()
        sandbox.set_data(
            [(Path(config.dataset_path), SANDBOX_DATA_PATH)]
            + ([(Path(config.reference_dataset_path), SANDBOX_REF_DATA_PATH)]
               if config.reference_dataset_path else []),
            output_dir,
        )
        if not sandbox.start_container():
            raise RuntimeError("Singularity sandbox failed to start.")
        return sandbox

    raise ValueError(f"Unknown sandbox type: {config.sandbox_type}")
