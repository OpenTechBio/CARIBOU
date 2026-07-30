from __future__ import annotations

from types import SimpleNamespace

import pytest

from caribou.core import openrouter
from caribou.core.openrouter import (
    OpenRouterError,
    create_openrouter_client,
    get_openrouter_catalogue,
    get_openrouter_endpoints,
    validate_openrouter_model_id,
)
from caribou.control.records import ProviderCallReceiptV2, ProviderCallUsage
from datetime import datetime, timezone
import asyncio
from caribou.server.session_setup import resolve_model_info
from caribou.server.routes.config import get_backends


def _catalogue_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "anthropic/claude-test",
                "canonical_slug": "anthropic/claude-test-20260720",
                "name": "Claude Test",
                "context_length": 128_000,
                "pricing": {"prompt": "0.000001"},
                "supported_parameters": ["max_tokens", "temperature"],
                "architecture": {"output_modalities": ["text"]},
                "expiration_date": None,
            },
            {
                "id": "image/only",
                "name": "Image only",
                "architecture": {"output_modalities": ["image"]},
            },
        ]
    }


def test_account_catalogue_parses_text_models_and_uses_cache(monkeypatch) -> None:
    monkeypatch.setattr(openrouter, "_catalogue_cache", None)
    calls: list[str] = []

    def request(url: str, key: str):
        calls.append(f"{url}:{key}")
        return _catalogue_payload()

    first = get_openrouter_catalogue("secret", request_json=request, now=lambda: 100.0)
    second = get_openrouter_catalogue("secret", request_json=request, now=lambda: 101.0)

    assert [model.canonical_slug for model in first.models] == [
        "anthropic/claude-test-20260720"
    ]
    assert second is first
    assert len(calls) == 1


def test_catalogue_uses_stale_success_after_refresh_failure(monkeypatch) -> None:
    monkeypatch.setattr(openrouter, "_catalogue_cache", None)
    get_openrouter_catalogue(
        "secret", request_json=lambda _url, _key: _catalogue_payload(), now=lambda: 1.0
    )

    def unavailable(_url: str, _key: str):
        raise OpenRouterError("offline")

    stale = get_openrouter_catalogue(
        "secret", refresh=True, request_json=unavailable, now=lambda: 2.0
    )
    assert stale.stale is True
    assert stale.fetched_at == 1.0


def test_strict_model_validation_rejects_moving_routes() -> None:
    assert (
        validate_openrouter_model_id("openai/gpt-fixed", strict=True)
        == "openai/gpt-fixed"
    )
    for value in ("~openai/gpt-latest", "openrouter/auto", "openrouter/free"):
        with pytest.raises(OpenRouterError):
            validate_openrouter_model_id(value, strict=True)


def test_endpoint_parser_returns_frozen_provider_slug() -> None:
    endpoints = get_openrouter_endpoints(
        "secret",
        "openai/gpt-fixed",
        request_json=lambda _url, _key: {
            "data": {
                "endpoints": [
                    {
                        "name": "Example Inference",
                        "provider_name": "example",
                        "provider_slug": "example/turbo",
                        "context_length": 32_000,
                        "pricing": {"prompt": "0.1"},
                    }
                ]
            }
        },
    )
    assert endpoints[0].slug == "example/turbo"


def test_client_injects_privacy_and_strict_routing(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Completions:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

    raw = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: raw)
    client = create_openrouter_client("secret", endpoint="example/turbo", max_retries=0)
    client.chat.completions.create(model="openai/gpt-fixed", messages=[])
    assert captured["extra_body"] == {
        "provider": {
            "zdr": True,
            "data_collection": "deny",
            "order": ["example/turbo"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }


def test_web_session_records_openrouter_identity() -> None:
    config = SimpleNamespace(
        llm_backend="openrouter", model_name="openai/gpt-fixed", ollama_model=None
    )
    resolved = resolve_model_info(config)
    assert resolved is not None
    assert resolved.provider == "openrouter"
    assert resolved.model == "openai/gpt-fixed"
    assert resolved.parameters["zdr"] is True


def test_v2_receipt_records_provider_reported_cost() -> None:
    now = datetime.now(timezone.utc)
    receipt = ProviderCallReceiptV2(
        call_id="run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:turn:1:attempt:1",
        run_id="run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        turn=1,
        agent_name="agent",
        attempt=1,
        maximum_attempts=1,
        provider="openrouter",
        requested_model="openai/gpt-fixed",
        outcome="succeeded",
        started_at=now,
        ended_at=now,
        duration_ms=1,
        response_id="generation-id",
        response_model="openai/gpt-fixed",
        upstream_provider="OpenAI",
        usage=ProviderCallUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost_usd=0.001,
        upstream_cost_usd=0.0009,
    )
    assert receipt.schema_version == "caribou.provider_call_receipt.v2"
    assert receipt.cost_usd == 0.001


def test_openrouter_backend_remains_discoverable_without_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "caribou.server.routes.config.ENV_FILE", tmp_path / "missing.env"
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    backends = asyncio.run(get_backends())
    backend = next(item for item in backends if item.id == "openrouter")
    assert backend.available is False
    assert backend.status == "not_configured"
    assert "set-openrouter-key" in (backend.suggested_fix or "")
