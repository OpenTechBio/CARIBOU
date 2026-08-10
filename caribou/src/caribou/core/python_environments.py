"""Discover and validate host Python environments for sandbox sessions.

This module intentionally performs shallow discovery only.  It queries environment
manager registries and well-known active-prefix variables; it never recursively
walks a user's home or shared HPC filesystems.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, Field


class PythonEnvironmentKind(str, Enum):
    conda = "conda"
    venv = "venv"
    pyenv = "pyenv"
    unknown = "unknown"


class PythonEnvironmentCandidate(BaseModel):
    """One usable host prefix returned by discovery or manual validation."""

    name: str
    path: str
    python_executable: str
    kind: PythonEnvironmentKind
    sources: list[str] = Field(default_factory=list)


class ResolvedPythonEnvironment(BaseModel):
    """Actual interpreter identity attached to a session attempt."""

    mode: Literal["bundled", "host"] = "bundled"
    path: str | None = None
    python_executable: str
    kind: PythonEnvironmentKind | None = None
    python_version: str | None = None
    fingerprint: str | None = None


def bundled_python_environment() -> ResolvedPythonEnvironment:
    return ResolvedPythonEnvironment(
        mode="bundled",
        python_executable="/usr/local/envs/rapids/bin/python",
        kind=PythonEnvironmentKind.conda,
    )


class PythonEnvironmentError(ValueError):
    """A requested host environment is structurally invalid on the host."""

    code = "PYTHON_ENV_INVALID"


class PythonEnvironmentChangedError(RuntimeError):
    """A mutable host prefix changed after its session identity was recorded."""

    code = "PYTHON_ENV_CHANGED"
    suggested_fix = (
        "Restore the original host environment or create a new session with the "
        "updated environment."
    )


_RESERVED_PREFIXES = {
    Path("/"),
    Path("/bin"),
    Path("/dev"),
    Path("/etc"),
    Path("/home"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt"),
    Path("/proc"),
    Path("/sbin"),
    Path("/sys"),
    Path("/tmp"),
    Path("/usr"),
    Path("/usr/local"),
    Path("/var"),
    Path("/workspace"),
}


def _environment_kind(prefix: Path, source: str | None = None) -> PythonEnvironmentKind:
    if (prefix / "conda-meta").is_dir():
        return PythonEnvironmentKind.conda
    if (prefix / "pyvenv.cfg").is_file():
        return PythonEnvironmentKind.venv
    if source == "pyenv" or "/.pyenv/versions/" in f"{prefix}/":
        return PythonEnvironmentKind.pyenv
    return PythonEnvironmentKind.unknown


def validate_python_environment_path(
    raw_path: str | os.PathLike[str], *, source: str | None = None
) -> PythonEnvironmentCandidate:
    """Validate a prefix without executing its Python interpreter."""

    path_text = os.fspath(raw_path).strip()
    if not path_text or "\x00" in path_text:
        raise PythonEnvironmentError("Python environment path is empty or malformed.")
    expanded = Path(path_text).expanduser()
    if not expanded.is_absolute():
        raise PythonEnvironmentError("Python environment path must be absolute.")
    try:
        prefix = expanded.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PythonEnvironmentError(
            f"Python environment does not exist or cannot be resolved: {expanded}"
        ) from exc
    if prefix in _RESERVED_PREFIXES or not prefix.is_dir():
        raise PythonEnvironmentError(
            "Python environment must be a dedicated prefix directory, not a system root."
        )
    if any(ord(character) < 32 for character in str(prefix)) or ":" in str(prefix):
        raise PythonEnvironmentError(
            "Python environment path contains characters unsupported by container binds."
        )
    python = prefix / "bin" / "python"
    if not python.exists() or not python.is_file() or not os.access(python, os.X_OK):
        raise PythonEnvironmentError(
            f"Python environment must contain an executable bin/python: {prefix}"
        )
    if not os.access(prefix, os.R_OK | os.X_OK):
        raise PythonEnvironmentError(f"Python environment is not readable: {prefix}")
    kind = _environment_kind(prefix, source)
    return PythonEnvironmentCandidate(
        name=prefix.name,
        path=str(prefix),
        python_executable=str(python),
        kind=kind,
        sources=[source] if source else ["manual"],
    )


def _manager_env_paths(command: str) -> list[str]:
    executable = shutil.which(command)
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "env", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return []
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    envs = payload.get("envs", []) if isinstance(payload, dict) else []
    return [item for item in envs if isinstance(item, str)]


def _registry_paths() -> list[str]:
    registry = Path.home() / ".conda" / "environments.txt"
    try:
        return [line.strip() for line in registry.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def _pyenv_paths() -> list[str]:
    root_text = os.environ.get("PYENV_ROOT")
    executable = shutil.which("pyenv")
    if executable:
        try:
            result = subprocess.run(
                [executable, "root"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                root_text = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    root = Path(root_text).expanduser() if root_text else Path.home() / ".pyenv"
    versions = root / "versions"
    try:
        return [str(item) for item in versions.iterdir() if item.is_dir()]
    except OSError:
        return []


def _root_prefix_paths(variable: str) -> list[str]:
    value = os.environ.get(variable)
    if not value:
        return []
    root = Path(value).expanduser()
    paths = [str(root)]
    envs = root / "envs"
    try:
        paths.extend(str(item) for item in envs.iterdir() if item.is_dir())
    except OSError:
        pass
    return paths


def discover_python_environments() -> list[PythonEnvironmentCandidate]:
    """Return validated host prefixes, deduplicated by canonical path."""

    discovered: list[tuple[str, str]] = []
    for variable, source in (
        ("CONDA_PREFIX", "active-conda"),
        ("VIRTUAL_ENV", "active-venv"),
    ):
        if value := os.environ.get(variable):
            discovered.append((value, source))
    for manager in ("conda", "mamba", "micromamba"):
        discovered.extend((path, manager) for path in _manager_env_paths(manager))
    discovered.extend((path, "conda-registry") for path in _registry_paths())
    discovered.extend((path, "mamba-root") for path in _root_prefix_paths("MAMBA_ROOT_PREFIX"))
    discovered.extend((path, "pyenv") for path in _pyenv_paths())

    by_path: dict[str, PythonEnvironmentCandidate] = {}
    for path, source in discovered:
        try:
            candidate = validate_python_environment_path(path, source=source)
        except PythonEnvironmentError:
            continue
        existing = by_path.get(candidate.path)
        if existing is None:
            by_path[candidate.path] = candidate
        elif source not in existing.sources:
            existing.sources.append(source)
        if existing is not None and existing.kind is PythonEnvironmentKind.unknown:
            inferred = _environment_kind(Path(existing.path), source)
            if inferred is not PythonEnvironmentKind.unknown:
                existing.kind = inferred

    active_paths: set[str] = set()
    for key in ("CONDA_PREFIX", "VIRTUAL_ENV"):
        value = os.environ.get(key)
        if not value:
            continue
        try:
            active_paths.add(str(Path(value).expanduser().resolve(strict=True)))
        except (OSError, RuntimeError):
            continue
    return sorted(
        by_path.values(),
        key=lambda item: (item.path not in active_paths, item.name.lower(), item.path),
    )


def _hash_files(paths: Iterable[Path]) -> str | None:
    digest = hashlib.sha256()
    found = False
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        found = True
        digest.update(str(path.name).encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest() if found else None


def environment_fingerprint(candidate: PythonEnvironmentCandidate) -> str | None:
    """Create a lightweight metadata fingerprint without hashing all packages."""

    prefix = Path(candidate.path)
    if candidate.kind is PythonEnvironmentKind.conda:
        history = prefix / "conda-meta" / "history"
        metadata = sorted((prefix / "conda-meta").glob("*.json"))
        return _hash_files([history, *metadata])
    if candidate.kind is PythonEnvironmentKind.venv:
        return _hash_files([prefix / "pyvenv.cfg"])
    try:
        stat = (prefix / "bin" / "python").stat()
    except OSError:
        return None
    return hashlib.sha256(
        f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()


def resolved_host_environment(
    candidate: PythonEnvironmentCandidate, *, python_version: str | None = None
) -> ResolvedPythonEnvironment:
    return ResolvedPythonEnvironment(
        mode="host",
        path=candidate.path,
        python_executable=candidate.python_executable,
        kind=candidate.kind,
        python_version=python_version,
        fingerprint=environment_fingerprint(candidate),
    )


def assert_environment_unchanged(
    expected: ResolvedPythonEnvironment,
    actual: ResolvedPythonEnvironment,
) -> None:
    """Reject a changed host prefix rather than silently altering run semantics."""

    if expected.mode != "host":
        return
    if actual.mode != "host" or expected.path != actual.path:
        raise PythonEnvironmentChangedError(
            "Selected host Python environment resolved to a different prefix."
        )
    if (
        expected.fingerprint
        and actual.fingerprint
        and expected.fingerprint != actual.fingerprint
    ):
        raise PythonEnvironmentChangedError(
            f"Selected host Python environment changed since it was recorded: {expected.path}"
        )
