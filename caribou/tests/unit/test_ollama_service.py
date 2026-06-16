import requests
import pytest

from caribou.server import ollama_service
from caribou.server.ollama_service import OllamaStartupError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def setup_function():
    ollama_service._owned_process = None


def test_probe_returns_downloaded_models(monkeypatch):
    monkeypatch.setattr(
        ollama_service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({
            "models": [{"name": "llama3"}, {"name": "deepseek-r1:70b"}]
        }),
    )

    status = ollama_service.probe_ollama("localhost:11434")

    assert status.running is True
    assert status.status == "ready"
    assert status.models == ["deepseek-r1:70b", "llama3"]


def test_probe_reports_no_models(monkeypatch):
    monkeypatch.setattr(
        ollama_service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"models": []}),
    )

    status = ollama_service.probe_ollama("http://localhost:11434")

    assert status.running is True
    assert status.status == "no_models"
    assert "ollama pull" in status.suggested_fix


def test_ensure_starts_local_ollama_when_installed(monkeypatch):
    responses = [
        requests.ConnectionError("down"),
        FakeResponse({"models": [{"name": "llama3"}]}),
    ]
    process = FakeProcess()

    def fake_get(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ollama_service.requests, "get", fake_get)
    monkeypatch.setattr(ollama_service, "which", lambda cmd: "/usr/bin/ollama")
    monkeypatch.setattr(ollama_service.subprocess, "Popen", lambda *args, **kwargs: process)

    host, model = ollama_service.ensure_ollama_ready("http://localhost:11434", "llama3")

    assert host == "http://localhost:11434"
    assert model == "llama3"
    assert ollama_service._owned_process is process


def test_ensure_missing_executable_raises_not_installed(monkeypatch):
    monkeypatch.setattr(
        ollama_service.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    monkeypatch.setattr(ollama_service, "which", lambda cmd: None)

    with pytest.raises(OllamaStartupError) as exc:
        ollama_service.ensure_ollama_ready("http://localhost:11434", "llama3")

    assert exc.value.code == "OLLAMA_NOT_INSTALLED"


def test_ensure_remote_host_never_spawns(monkeypatch):
    monkeypatch.setattr(
        ollama_service.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    monkeypatch.setattr(
        ollama_service.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    with pytest.raises(OllamaStartupError) as exc:
        ollama_service.ensure_ollama_ready("http://ollama.example:11434", "llama3")

    assert exc.value.code == "OLLAMA_UNREACHABLE"


def test_ensure_missing_model_reports_pull_command(monkeypatch):
    monkeypatch.setattr(
        ollama_service.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"models": [{"name": "llama3"}]}),
    )

    with pytest.raises(OllamaStartupError) as exc:
        ollama_service.ensure_ollama_ready("http://localhost:11434", "mistral")

    assert exc.value.code == "OLLAMA_MODEL_MISSING"
    assert exc.value.suggested_fix == "Download it with: ollama pull mistral"


def test_shutdown_owned_ollama_terminates_only_tracked_process():
    process = FakeProcess()
    ollama_service._owned_process = process

    ollama_service.shutdown_owned_ollama()

    assert process.terminated is True
    assert ollama_service._owned_process is None
