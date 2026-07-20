"""Shared experiment-control bearer token resolution and persistence."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, set_key

from caribou.config import ENV_FILE


CONTROL_TOKEN_ENV = "CARIBOU_CONTROL_API_TOKEN"
ControlTokenSource = Literal["environment", "caribou_env_file", "generated"]


@dataclass(frozen=True)
class ControlAccessToken:
    """Resolved control token and enough source metadata for honest CLI output."""

    value: str
    source: ControlTokenSource
    env_file: Path | None = None

    @property
    def generated(self) -> bool:
        return self.source == "generated"


def resolve_control_access_token(
    *,
    create: bool,
    env_file: Path | None = None,
    environment: MutableMapping[str, str] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> ControlAccessToken | None:
    """Resolve the configured token, optionally generating and persisting one.

    The live process environment takes precedence over CARIBOU's protected
    ``.env`` file. Generated tokens are written to that file with mode ``0600``
    and exported into the current process so uvicorn workers inherit the exact
    value displayed by ``caribou serve``.
    """

    target_env = environment if environment is not None else os.environ
    configured = target_env.get(CONTROL_TOKEN_ENV, "").strip()
    if configured:
        return ControlAccessToken(value=configured, source="environment")

    target_file = env_file if env_file is not None else ENV_FILE
    if target_file.exists():
        file_value = dotenv_values(target_file).get(CONTROL_TOKEN_ENV)
        configured = file_value.strip() if isinstance(file_value, str) else ""
        if configured:
            target_env[CONTROL_TOKEN_ENV] = configured
            return ControlAccessToken(
                value=configured,
                source="caribou_env_file",
                env_file=target_file,
            )

    if not create:
        return None

    generator = token_factory or (lambda: secrets.token_urlsafe(32))
    generated = generator().strip()
    if not generated:
        raise RuntimeError("control token generator returned an empty value")

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.touch(mode=0o600, exist_ok=True)
    os.chmod(target_file, 0o600)
    set_key(str(target_file), CONTROL_TOKEN_ENV, generated, quote_mode="always")
    os.chmod(target_file, 0o600)
    target_env[CONTROL_TOKEN_ENV] = generated
    return ControlAccessToken(
        value=generated,
        source="generated",
        env_file=target_file,
    )
