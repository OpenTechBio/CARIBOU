"""Shared OpenRouter inference, catalogue, and reproducibility policy."""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


OPENROUTER_API_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CATALOG_URL = "https://openrouter.ai/models"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_HTTP_REFERER = "https://github.com/peerdavid/CARIBOU"
OPENROUTER_APP_TITLE = "CARIBOU"
OPENROUTER_CACHE_SECONDS = 300

_DYNAMIC_MODEL_IDS = {
    "openrouter/auto",
    "openrouter/free",
}


class OpenRouterError(RuntimeError):
    """An actionable OpenRouter configuration or catalogue failure."""


@dataclass(frozen=True)
class OpenRouterModel:
    id: str
    canonical_slug: str
    name: str
    context_length: int | None
    pricing: dict[str, str]
    supported_parameters: tuple[str, ...]
    description: str | None
    expiration_date: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "canonical_slug": self.canonical_slug,
            "name": self.name,
            "context_length": self.context_length,
            "pricing": self.pricing,
            "supported_parameters": list(self.supported_parameters),
            "description": self.description,
            "expiration_date": self.expiration_date,
        }


@dataclass(frozen=True)
class OpenRouterEndpoint:
    slug: str
    name: str
    context_length: int | None
    pricing: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "name": self.name,
            "context_length": self.context_length,
            "pricing": self.pricing,
        }


@dataclass(frozen=True)
class OpenRouterCatalogue:
    models: tuple[OpenRouterModel, ...]
    fetched_at: float
    stale: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "models": [model.as_dict() for model in self.models],
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "catalog_url": OPENROUTER_CATALOG_URL,
        }


_catalogue_cache: OpenRouterCatalogue | None = None
_catalogue_cache_key: str | None = None


def _json_request(url: str, api_key: str) -> Mapping[str, Any]:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "HTTP-Referer": OPENROUTER_HTTP_REFERER,
            "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
        },
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS host
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise OpenRouterError("OPENROUTER_API_KEY was rejected") from exc
        raise OpenRouterError(f"OpenRouter returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OpenRouterError(
            "OpenRouter catalogue is temporarily unavailable"
        ) from exc
    if not isinstance(payload, Mapping):
        raise OpenRouterError("OpenRouter returned an invalid catalogue response")
    return payload


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _parse_models(payload: Mapping[str, Any]) -> tuple[OpenRouterModel, ...]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise OpenRouterError("OpenRouter catalogue response has no model list")
    models: list[OpenRouterModel] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        canonical = item.get("canonical_slug") or model_id
        name = item.get("name") or model_id
        if not all(
            isinstance(value, str) and value for value in (model_id, canonical, name)
        ):
            continue
        architecture = item.get("architecture")
        if isinstance(architecture, Mapping):
            outputs = architecture.get("output_modalities")
            if isinstance(outputs, list) and "text" not in outputs:
                continue
        supported = item.get("supported_parameters")
        models.append(
            OpenRouterModel(
                id=model_id,
                canonical_slug=canonical,
                name=name,
                context_length=_optional_int(item.get("context_length")),
                pricing=_string_mapping(item.get("pricing")),
                supported_parameters=tuple(
                    value for value in supported or () if isinstance(value, str)
                ),
                description=item.get("description")
                if isinstance(item.get("description"), str)
                else None,
                expiration_date=(
                    item.get("expiration_date")
                    if isinstance(item.get("expiration_date"), str)
                    else None
                ),
            )
        )
    return tuple(models)


def get_openrouter_catalogue(
    api_key: str,
    *,
    refresh: bool = False,
    now: Callable[[], float] = time.time,
    request_json: Callable[[str, str], Mapping[str, Any]] = _json_request,
) -> OpenRouterCatalogue:
    """Return the account-filtered model catalogue with a bounded stale fallback."""

    global _catalogue_cache, _catalogue_cache_key
    current = now()
    key_identity = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    if (
        not refresh
        and _catalogue_cache is not None
        and _catalogue_cache_key == key_identity
        and current - _catalogue_cache.fetched_at < OPENROUTER_CACHE_SECONDS
    ):
        return _catalogue_cache
    try:
        payload = request_json(f"{OPENROUTER_API_BASE_URL}/models/user", api_key)
        catalogue = OpenRouterCatalogue(_parse_models(payload), fetched_at=current)
        _catalogue_cache = catalogue
        _catalogue_cache_key = key_identity
        return catalogue
    except OpenRouterError:
        if _catalogue_cache is not None and _catalogue_cache_key == key_identity:
            return OpenRouterCatalogue(
                _catalogue_cache.models,
                fetched_at=_catalogue_cache.fetched_at,
                stale=True,
            )
        raise


def get_openrouter_endpoints(
    api_key: str,
    model_id: str,
    *,
    request_json: Callable[[str, str], Mapping[str, Any]] = _json_request,
) -> tuple[OpenRouterEndpoint, ...]:
    canonical = validate_openrouter_model_id(model_id, strict=True)
    author, slug = canonical.split("/", 1)
    payload = request_json(
        f"{OPENROUTER_API_BASE_URL}/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints",
        api_key,
    )
    data = payload.get("data")
    raw_endpoints = data.get("endpoints") if isinstance(data, Mapping) else None
    if not isinstance(raw_endpoints, list):
        raise OpenRouterError("OpenRouter returned no endpoint list for this model")
    endpoints: list[OpenRouterEndpoint] = []
    for item in raw_endpoints:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("provider_name")
        slug_value = (
            item.get("tag") or item.get("provider_slug") or item.get("provider_name")
        )
        if (
            isinstance(name, str)
            and name
            and isinstance(slug_value, str)
            and slug_value
        ):
            endpoints.append(
                OpenRouterEndpoint(
                    slug=slug_value,
                    name=name,
                    context_length=_optional_int(item.get("context_length")),
                    pricing=_string_mapping(item.get("pricing")),
                )
            )
    return tuple(endpoints)


def validate_openrouter_model_id(model_id: str, *, strict: bool) -> str:
    value = model_id.strip()
    if not value or "/" not in value or any(character.isspace() for character in value):
        raise OpenRouterError("OpenRouter model must use the exact author/model slug")
    if strict and (value.startswith("~") or value in _DYNAMIC_MODEL_IDS):
        raise OpenRouterError("dynamic OpenRouter model aliases are not reproducible")
    return value


def openrouter_routing(*, endpoint: str | None = None) -> dict[str, object]:
    routing: dict[str, object] = {
        "zdr": True,
        "data_collection": "deny",
    }
    if endpoint:
        routing.update(
            {
                "order": [endpoint],
                "allow_fallbacks": False,
                "require_parameters": True,
            }
        )
    return routing


class _OpenRouterCompletions:
    def __init__(self, completions: object, routing: Mapping[str, object]) -> None:
        self._completions = completions
        self._routing = dict(routing)

    def create(self, *args: object, **kwargs: object) -> object:
        extra_body = kwargs.get("extra_body")
        if extra_body is None:
            body: dict[str, object] = {}
        elif isinstance(extra_body, Mapping):
            body = dict(extra_body)
        else:
            raise TypeError("OpenRouter extra_body must be a mapping")
        requested = body.get("provider")
        provider = dict(requested) if isinstance(requested, Mapping) else {}
        provider.update(self._routing)
        body["provider"] = provider
        kwargs["extra_body"] = body
        return self._completions.create(*args, **kwargs)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class _OpenRouterChat:
    completions: _OpenRouterCompletions


class OpenRouterClient:
    def __init__(self, raw_client: object, routing: Mapping[str, object]) -> None:
        self.raw_client = raw_client
        self.chat = _OpenRouterChat(
            completions=_OpenRouterCompletions(raw_client.chat.completions, routing)  # type: ignore[attr-defined]
        )


def create_openrouter_client(
    api_key: str,
    *,
    endpoint: str | None = None,
    max_retries: int | None = None,
) -> OpenRouterClient:
    from openai import OpenAI

    options: dict[str, object] = {
        "api_key": api_key,
        "base_url": OPENROUTER_API_BASE_URL,
        "default_headers": {
            "HTTP-Referer": OPENROUTER_HTTP_REFERER,
            "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
        },
    }
    if max_retries is not None:
        options["max_retries"] = max_retries
    return OpenRouterClient(OpenAI(**options), openrouter_routing(endpoint=endpoint))
