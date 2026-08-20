import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from caribou.server.models import PythonEnvironmentPathRequest
from caribou.server.models import SessionCreateRequest
from caribou.server.routes.config import validate_python_environment
from caribou.server.session_manager import SessionManager


def test_validate_python_environment_route_returns_canonical_prefix(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "env"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)

    result = asyncio.run(
        validate_python_environment(PythonEnvironmentPathRequest(path=str(prefix)))
    )

    assert result.path == str(prefix.resolve())


def test_validate_python_environment_route_returns_400_for_bad_prefix(
    tmp_path: Path,
) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            validate_python_environment(
                PythonEnvironmentPathRequest(path=str(tmp_path / "missing"))
            )
        )

    assert exc.value.status_code == 400


def test_session_creation_records_canonical_requested_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "host-env"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    manager = object.__new__(SessionManager)
    manager._sessions = {}
    manager._deleted_session_ids = set()
    manager._lock = asyncio.Lock()
    manager._save_session = lambda session: None

    async def initialize(session):
        return None

    manager._initialize_session = initialize
    monkeypatch.setattr("caribou.server.session_manager.SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(
        "caribou.server.session_manager._create_session_logger",
        lambda *args: SimpleNamespace(info=lambda *args: None),
    )
    config = SessionCreateRequest(
        mode="interactive",
        agent_system="caribou",
        llm_backend="chatgpt",
        sandbox_type="singularity",
        python_environment_path=str(prefix),
        dataset_path=str(tmp_path / "dataset.h5ad"),
    )

    response = asyncio.run(manager.create_session(config))

    session = manager.get_session(response.id)
    assert session is not None
    assert session.config.python_environment_path == str(prefix.resolve())
    assert response.python_environment.mode == "host"
    assert response.python_environment.path == str(prefix.resolve())
