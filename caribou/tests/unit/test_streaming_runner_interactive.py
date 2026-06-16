import queue
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

from caribou.server.streaming_runner import (
    _stream_tokens,
    _stream_tokens_with_fallback,
    run_session_sync,
)


class FakeAgent:
    def __init__(self, name, commands=None):
        self.name = name
        self.commands = commands or {}
        self.is_rag_enabled = False

    def get_full_prompt(self, _):
        return f"You are {self.name}."


class FakeAgentSystem:
    def __init__(self, agents):
        self.agents = agents

    def get_agent(self, name):
        return self.agents.get(name)


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **_kwargs):
        self.calls += 1
        text = self.responses.pop(0)

        class Chunk:
            def __init__(self, token):
                self.choices = [SimpleNamespace(delta=SimpleNamespace(content=token))]

        return [Chunk(text)]


class FakeSandbox:
    def exec_code(self, _code, timeout):
        return {"status": "ok", "stdout": "", "stderr": ""}


class FakeNonStreamingLLM:
    def __init__(self, text):
        self.text = text
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **_kwargs):
        message = SimpleNamespace(content=self.text, role="assistant")
        choice = SimpleNamespace(message=message, index=0, finish_reason="stop")
        return SimpleNamespace(choices=[choice])


class FakeFailingStreamingLLM:
    def __init__(self, *, fail_after_token=False):
        self.fail_after_token = fail_after_token
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)

        if kwargs.get("stream"):
            def chunks():
                if self.fail_after_token:
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"))])
                raise RuntimeError("incomplete chunked read")
            return chunks()

        message = SimpleNamespace(content="fallback response", role="assistant")
        choice = SimpleNamespace(message=message, index=0, finish_reason="stop")
        return SimpleNamespace(choices=[choice])


def test_stream_tokens_accepts_non_streaming_openai_compatible_response():
    llm = FakeNonStreamingLLM("loaded")

    assert list(_stream_tokens(llm, "llama3", [{"role": "user", "content": "load"}])) == ["loaded"]


def test_stream_tokens_with_fallback_retries_non_streaming_before_any_tokens():
    llm = FakeFailingStreamingLLM()

    assert list(_stream_tokens_with_fallback(llm, "deepseek-chat", [{"role": "user", "content": "hi"}])) == [
        "fallback response"
    ]
    assert llm.calls[0]["stream"] is True
    assert "stream" not in llm.calls[1]


def test_stream_tokens_with_fallback_recovers_after_partial_stream():
    llm = FakeFailingStreamingLLM(fail_after_token=True)

    chunks = list(_stream_tokens_with_fallback(llm, "deepseek-chat", [{"role": "user", "content": "hi"}]))

    assert chunks == [
        "partial",
        "\n\n[Streaming connection interrupted. Retrying request without streaming...]\n\n",
        "fallback response",
    ]


def test_interactive_delegation_waits_for_user_before_next_agent_turn(tmp_path: Path, monkeypatch):
    rag_stub = ModuleType("caribou.execution.rag_client")
    rag_stub.get_rag_client = lambda _console: None
    monkeypatch.setitem(__import__("sys").modules, "caribou.execution.rag_client", rag_stub)

    coder = FakeAgent("coder")
    planner = FakeAgent(
        "planner",
        {"delegate_to_coder": SimpleNamespace(target_agent="coder")},
    )
    agent_system = FakeAgentSystem({"planner": planner, "coder": coder})
    llm = FakeLLM(["delegate_to_coder", "coder should not run yet"])
    stop_flag = threading.Event()
    user_input_queue = queue.Queue()
    events = []
    idle_seen = threading.Event()

    def emit(event):
        events.append(event)
        if event["type"] == "status_change" and event["data"].get("status") == "idle":
            idle_seen.set()

    thread = threading.Thread(
        target=run_session_sync,
        kwargs={
            "session_id": "session-1",
            "agent_system": agent_system,
            "driver_agent": planner,
            "analysis_context": "analysis",
            "llm_client": llm,
            "sandbox_manager": FakeSandbox(),
            "history": [{"role": "user", "content": "start"}],
            "is_auto": False,
            "max_turns": 10,
            "model_name": "fake",
            "output_dir": tmp_path,
            "emit": emit,
            "stop_flag": stop_flag,
            "user_input_queue": user_input_queue,
        },
    )
    thread.start()

    assert idle_seen.wait(timeout=2)
    assert llm.calls == 1
    assert [
        event["data"]["message"]["content"]
        for event in events
        if event["type"] == "message_complete"
    ] == ["delegate_to_coder"]
    assert any(event["type"] == "agent_switch" for event in events)

    stop_flag.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
