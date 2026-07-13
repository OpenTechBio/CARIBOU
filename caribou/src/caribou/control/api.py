"""Stable machine-response and error contract for agent-facing commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from enum import IntEnum
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping, NoReturn


MACHINE_RESPONSE_VERSION = "caribou.machine_response.v1"


class ExitCode(IntEnum):
    """Stable process outcomes for the agent-facing CLI."""

    success = 0
    usage = 2
    validation = 10
    not_found = 11
    conflict = 12
    permission = 13
    budget = 14
    transient = 15
    execution = 16
    cancelled = 17
    internal = 18
    integrity = 19


class ControlError(RuntimeError):
    """Structured failure that can cross a CLI or API transport boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: ExitCode,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.details = dict(details or {})


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def caribou_version() -> str:
    try:
        return metadata.version("caribou")
    except metadata.PackageNotFoundError:
        return "0.1.0"


def code_commit() -> str:
    """Resolve the executing code identity without requiring caller knowledge."""

    configured = os.environ.get("CARIBOU_CODE_COMMIT")
    if configured:
        return configured
    for parent in Path(__file__).resolve().parents:
        if not (parent / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        candidate = result.stdout.strip()
        if result.returncode == 0 and len(candidate) == 40:
            return candidate
    return "unresolved"


def machine_response(
    command: str,
    *,
    data: Mapping[str, Any],
    object_type: str,
    object_id: str,
    state: str,
    links: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the one-object stdout envelope used by non-streaming commands."""

    return {
        "schema_version": MACHINE_RESPONSE_VERSION,
        "command": command,
        "ok": True,
        "timestamp": utc_timestamp(),
        "caribou": {
            "version": caribou_version(),
            "commit": code_commit(),
        },
        "object": {
            "type": object_type,
            "id": object_id,
            "state": state,
        },
        "data": dict(data),
        "links": dict(links or {}),
    }


def error_response(command: str, error: ControlError) -> dict[str, Any]:
    return {
        "schema_version": MACHINE_RESPONSE_VERSION,
        "command": command,
        "ok": False,
        "timestamp": utc_timestamp(),
        "caribou": {
            "version": caribou_version(),
            "commit": code_commit(),
        },
        "object": None,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        },
        "links": {},
    }


def emit_json(value: Mapping[str, Any]) -> None:
    """Write exactly one JSON object to stdout."""

    sys.stdout.write(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    )
    sys.stdout.flush()


def fail_json(command: str, error: ControlError) -> NoReturn:
    emit_json(error_response(command, error))
    raise SystemExit(int(error.exit_code))
