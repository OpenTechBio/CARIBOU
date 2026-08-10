from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from caribou.core import python_environments
from caribou.core.python_environments import (
    PythonEnvironmentChangedError,
    PythonEnvironmentError,
    PythonEnvironmentKind,
    discover_python_environments,
    environment_fingerprint,
    assert_environment_unchanged,
    resolved_host_environment,
    validate_python_environment_path,
)


def _make_environment(root: Path, *, conda: bool = False, venv: bool = False) -> Path:
    python = root / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    if conda:
        metadata = root / "conda-meta"
        metadata.mkdir()
        (metadata / "history").write_text("+python-3.12\n")
        (metadata / "python.json").write_text('{"name":"python"}\n')
    if venv:
        (root / "pyvenv.cfg").write_text("version = 3.12\n")
    return root


def test_validate_conda_environment_and_fingerprint(tmp_path: Path) -> None:
    prefix = _make_environment(tmp_path / "analysis", conda=True)

    candidate = validate_python_environment_path(prefix)

    assert candidate.path == str(prefix.resolve())
    assert candidate.python_executable == str(prefix.resolve() / "bin" / "python")
    assert candidate.kind is PythonEnvironmentKind.conda
    assert environment_fingerprint(candidate)


@pytest.mark.parametrize("path", ["", "relative/environment", "/", "/usr"])
def test_validate_environment_rejects_invalid_or_broad_paths(path: str) -> None:
    with pytest.raises(PythonEnvironmentError):
        validate_python_environment_path(path)


def test_validate_environment_requires_executable_python(tmp_path: Path) -> None:
    prefix = tmp_path / "broken"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_text("not executable")

    with pytest.raises(PythonEnvironmentError, match="executable bin/python"):
        validate_python_environment_path(prefix)


def test_discovery_deduplicates_manager_and_active_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _make_environment(tmp_path / "active", conda=True)
    other = _make_environment(tmp_path / "other", venv=True)
    registry = tmp_path / ".conda" / "environments.txt"
    registry.parent.mkdir()
    registry.write_text(f"{active}\n{other}\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CONDA_PREFIX", str(active))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("MAMBA_ROOT_PREFIX", raising=False)
    monkeypatch.delenv("PYENV_ROOT", raising=False)
    monkeypatch.setattr(
        python_environments.shutil,
        "which",
        lambda command: f"/tools/{command}" if command in {"conda", "mamba"} else None,
    )

    def fake_run(command, **kwargs):
        assert kwargs["timeout"] == 3
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"envs": [str(active), str(other)]}),
        )

    monkeypatch.setattr(python_environments.subprocess, "run", fake_run)

    discovered = discover_python_environments()

    assert [item.path for item in discovered] == [str(active), str(other)]
    assert set(discovered[0].sources) == {"active-conda", "conda", "mamba", "conda-registry"}
    assert discovered[1].kind is PythonEnvironmentKind.venv


def test_discovery_tolerates_stale_active_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDA_PREFIX", "/missing/environment")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("MAMBA_ROOT_PREFIX", raising=False)
    monkeypatch.delenv("PYENV_ROOT", raising=False)
    monkeypatch.setattr(python_environments.shutil, "which", lambda command: None)
    monkeypatch.setattr(python_environments, "_registry_paths", lambda: [])
    monkeypatch.setattr(python_environments, "_pyenv_paths", lambda: [])

    assert discover_python_environments() == []


def test_changed_environment_fingerprint_is_rejected(tmp_path: Path) -> None:
    prefix = _make_environment(tmp_path / "analysis", conda=True)
    candidate = validate_python_environment_path(prefix)
    expected = resolved_host_environment(candidate)
    (prefix / "conda-meta" / "history").write_text("+python-3.12\n+numpy-2\n")
    actual = resolved_host_environment(validate_python_environment_path(prefix))

    with pytest.raises(PythonEnvironmentChangedError, match="changed"):
        assert_environment_unchanged(expected, actual)
