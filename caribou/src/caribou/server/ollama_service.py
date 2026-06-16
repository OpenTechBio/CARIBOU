from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from shutil import which
from typing import Optional
from urllib.parse import urlparse

import requests


DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3"
_PROBE_TIMEOUT_SECONDS = 2
_START_TIMEOUT_SECONDS = 15
_owned_process: Optional[subprocess.Popen] = None


@dataclass
class OllamaStatus:
    host: str
    running: bool
    models: list[str]
    status: str
    message: str
    suggested_fix: Optional[str] = None


class OllamaStartupError(RuntimeError):
    def __init__(self, code: str, message: str, suggested_fix: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.suggested_fix = suggested_fix


def normalize_host(host: str | None) -> str:
    value = (host or DEFAULT_OLLAMA_HOST).strip()
    if not value.startswith(("http://", "https://")):
        value = "http://" + value
    return value.rstrip("/")


def is_local_host(host: str) -> bool:
    parsed = urlparse(normalize_host(host))
    hostname = (parsed.hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def probe_ollama(host: str | None) -> OllamaStatus:
    host = normalize_host(host)
    try:
        response = requests.get(f"{host}/api/tags", timeout=_PROBE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        if is_local_host(host):
            if which("ollama"):
                return OllamaStatus(
                    host=host,
                    running=False,
                    models=[],
                    status="not_running",
                    message="Ollama is installed but is not responding.",
                    suggested_fix="CARIBOU will try to start it when you create an Ollama session, or run: ollama serve",
                )
            return OllamaStatus(
                host=host,
                running=False,
                models=[],
                status="not_installed",
                message="The ollama executable was not found on this server.",
                suggested_fix="Install Ollama on the CARIBOU server host, then run: ollama serve",
            )
        return OllamaStatus(
            host=host,
            running=False,
            models=[],
            status="unreachable",
            message=f"Ollama did not respond at {host}: {exc}",
            suggested_fix="Check OLLAMA_HOST and make sure that Ollama is reachable from the CARIBOU server.",
        )

    models = _extract_model_names(response.json())
    if not models:
        return OllamaStatus(
            host=host,
            running=True,
            models=[],
            status="no_models",
            message="Ollama is running, but no downloaded models were found.",
            suggested_fix=f"Download a model with: ollama pull {DEFAULT_OLLAMA_MODEL}",
        )
    return OllamaStatus(
        host=host,
        running=True,
        models=models,
        status="ready",
        message="Ollama is running.",
    )


def ensure_ollama_ready(host: str | None, requested_model: str | None) -> tuple[str, str]:
    host = normalize_host(host)
    status = probe_ollama(host)

    if status.status == "not_running" and is_local_host(host):
        _start_ollama_process()
        status = _wait_for_ollama(host)

    if status.status == "not_installed":
        raise OllamaStartupError("OLLAMA_NOT_INSTALLED", status.message, status.suggested_fix)
    if status.status == "unreachable":
        raise OllamaStartupError("OLLAMA_UNREACHABLE", status.message, status.suggested_fix)
    if status.status == "not_running":
        raise OllamaStartupError("OLLAMA_UNREACHABLE", status.message, status.suggested_fix)
    if status.status == "no_models":
        raise OllamaStartupError("OLLAMA_NO_MODELS", status.message, status.suggested_fix)
    if status.status != "ready":
        raise OllamaStartupError("OLLAMA_START_FAILED", status.message, status.suggested_fix)

    model = (requested_model or "").strip()
    if not model:
        model = status.models[0]
    if model not in status.models:
        raise OllamaStartupError(
            "OLLAMA_MODEL_MISSING",
            f"Ollama model '{model}' is not downloaded on this server.",
            f"Download it with: ollama pull {model}",
        )
    return host, model


def shutdown_owned_ollama() -> None:
    global _owned_process
    proc = _owned_process
    _owned_process = None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _extract_model_names(payload: dict) -> list[str]:
    models = payload.get("models", [])
    names: list[str] = []
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict) and isinstance(model.get("name"), str):
                names.append(model["name"])
    return sorted(names)


def _start_ollama_process() -> None:
    global _owned_process
    if _owned_process and _owned_process.poll() is None:
        return
    if not which("ollama"):
        raise OllamaStartupError(
            "OLLAMA_NOT_INSTALLED",
            "The ollama executable was not found on this server.",
            "Install Ollama on the CARIBOU server host, then run: ollama serve",
        )
    try:
        _owned_process = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise OllamaStartupError(
            "OLLAMA_START_FAILED",
            f"Unable to start Ollama: {exc}",
            "Try starting Ollama manually with: ollama serve",
        ) from exc


def _wait_for_ollama(host: str) -> OllamaStatus:
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    last_status = probe_ollama(host)
    while time.monotonic() < deadline:
        if last_status.running:
            return last_status
        time.sleep(0.5)
        last_status = probe_ollama(host)
    raise OllamaStartupError(
        "OLLAMA_START_TIMEOUT",
        f"Ollama did not become ready at {host} within {_START_TIMEOUT_SECONDS} seconds.",
        "Try starting Ollama manually with: ollama serve",
    )
