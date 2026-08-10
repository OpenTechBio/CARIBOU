
import time
import os
import shutil
import hashlib
import selectors
import threading
from typing import List, Tuple, Dict, Optional
from pathlib import Path

import json

from caribou.config import CARIBOU_HOME
from caribou.core.python_environments import (
    PythonEnvironmentKind,
    ResolvedPythonEnvironment,
    resolved_host_environment,
    validate_python_environment_path,
)
from caribou.sandbox.benchmarking_sandbox_management import (
    SandboxManager as _BackendManager,
    CONTAINER_NAME as _SANDBOX_HANDLE,
    IMAGE_TAG as _SANDBOX_IMAGE,
    API_PORT_HOST as _API_PORT,
)


class SandboxReplUnavailableError(RuntimeError):
    """Raised when exec_code is called against a REPL already invalidated
    by a prior timeout or cancellation. A narrow subclass of RuntimeError
    so callers can recover from exactly this case without also catching
    unrelated RuntimeErrors (e.g. an unimplemented-backend NotImplementedError,
    which is itself a RuntimeError subclass, or a genuine sandbox-contract
    violation) that should still propagate as real bugs."""


def _nvidia_gpu_available() -> bool:
    """
    Check if NVIDIA GPU is actually available and accessible.
    Returns True only if nvidia-smi finds at least one GPU.
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return False
    try:
        import subprocess as sp
        # Use -L to list GPUs - output will contain "GPU 0:" if GPUs exist
        result = sp.run([nvidia_smi, "-L"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False
        # Check if output actually contains GPU entries
        # nvidia-smi -L returns lines like "GPU 0: NVIDIA A100..." for each GPU
        return "GPU " in result.stdout
    except Exception:
        return False


def init_docker(
    script_dir: str,
    subprocess,
    console,
    force_refresh: bool = False,
    *,
    python_environment_path: str | Path | None = None,
):
    # --- optional force‑refresh logic --------------------------------------
    if force_refresh:
        console.print("[yellow]Forcing Docker sandbox refresh…[/yellow]")
        # Stop & remove any running container gracefully
        subprocess.run(["docker", "rm", "-f", _SANDBOX_HANDLE], check=False)
        # Remove the sandbox image to ensure re‑pull/build
        subprocess.run(["docker", "image", "rm", "-f", _SANDBOX_IMAGE], check=False)
        console.print("[green]Docker image removed – it will be pulled/built on next start.[/green]")

    def COPY_CMD(src: str, dst: str):
        subprocess.run(["docker", "cp", src, dst], check=True)
    
    # create sandbox directory in docker 
    EXECUTE_ENDPOINT = f"http://localhost:{_API_PORT}/execute"
    STATUS_ENDPOINT = f"http://localhost:{_API_PORT}/status"

    class _ConfiguredDockerBackend(_BackendManager):
        def __init__(self):
            super().__init__()
            if python_environment_path:
                self.set_python_environment(python_environment_path)

    return _ConfiguredDockerBackend, _SANDBOX_HANDLE, COPY_CMD, EXECUTE_ENDPOINT, STATUS_ENDPOINT




def _normalise_sha256(value: str) -> str:
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("sif_sha256 must be a 64-character SHA-256 digest")
    return digest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def init_singularity_exec(
    script_dir: str,
    sanbox_data_path,
    subprocess,
    console,
    force_refresh: bool = False,
    *,
    sif_path: str | Path | None = None,
    sif_sha256: str | None = None,
    no_pull: bool = False,
    readiness_timeout: float = 30.0,
    gpu_enabled: bool | None = None,
    celltypist_cache_enabled: bool = True,
    python_environment_path: str | Path | None = None,
):
    """Configure the REPL, optionally using an existing hash-pinned SIF."""
    import caribou.sandbox.benchmarking_sandbox_management_singularity as sing

    if readiness_timeout <= 0:
        raise ValueError("readiness_timeout must be greater than zero")
    if sif_path is not None and sif_sha256 is None:
        raise ValueError("an explicit sif_path requires sif_sha256")

    default_sif_path = Path(sing.SIF_PATH).expanduser().resolve()
    SIF_PATH = Path(sif_path).expanduser().resolve() if sif_path else default_sif_path
    expected_sha256 = _normalise_sha256(sif_sha256) if sif_sha256 else None
    python_environment = (
        validate_python_environment_path(python_environment_path)
        if python_environment_path
        else None
    )

    if force_refresh and no_pull:
        raise ValueError("force_refresh and no_pull cannot be used together")
    if force_refresh and SIF_PATH != default_sif_path:
        raise ValueError("force_refresh is only supported for the legacy bundled SIF path")

    # optional force‑refresh
    if force_refresh:
        console.print("[yellow]Forcing Singularity sandbox refresh…[/yellow]")
        if SIF_PATH.exists():
            SIF_PATH.unlink()
            console.print(
                f"[green]Deleted {SIF_PATH.name} – it will be re‑downloaded on next start.[/green]"
            )

    SENTINEL = "<<<EOF>>>"

    # Shared CellTypist model cache — persists across all runs so models are
    # downloaded once and reused.  Bind-mounted to /workspace/celltypist_models.
    _celltypist_host_path: Path = CARIBOU_HOME / "celltypist_models"

    def ensure_sif() -> bool:
        if no_pull:
            if not SIF_PATH.is_file():
                console.print(f"[red]Pinned SIF is unavailable in no-pull mode: {SIF_PATH}[/red]")
                return False
        elif SIF_PATH == default_sif_path:
            if not sing.pull_sif_if_needed():
                return False
        elif not SIF_PATH.is_file():
            console.print(f"[red]Explicit SIF path does not exist: {SIF_PATH}[/red]")
            return False

        if expected_sha256 is not None:
            actual_sha256 = _sha256(SIF_PATH)
            if actual_sha256 != expected_sha256:
                console.print(
                    "[red]SIF SHA-256 mismatch; refusing to start. "
                    f"Expected {expected_sha256}, got {actual_sha256}.[/red]"
                )
                return False
        return True

    class _SingExecBackend:
        """Launch one long‑lived REPL inside the SIF and stream code to it."""

        image_path = SIF_PATH
        image_sha256 = expected_sha256
        image_no_pull = no_pull

        def __init__(self):
            self._binds: List[str] = []
            self._proc = None
            self._host_output_path: Optional[Path] = None
            self._stdout_buffer = bytearray()
            self.last_start_error: str | None = None
            self.python_environment = (
                resolved_host_environment(python_environment)
                if python_environment is not None
                else ResolvedPythonEnvironment(
                    mode="bundled",
                    python_executable="/usr/local/envs/rapids/bin/python",
                    kind=PythonEnvironmentKind.conda,
                )
            )

        def set_data(self, all_resources: List[Tuple[Path, str]], host_output_path: Path):
            """Configures all necessary bind mounts, including the output directory."""
            binds = []
            for host_path, container_path in all_resources:
                binds.extend(
                    ["--bind", f"{host_path.resolve()}:{container_path}:ro"]
                )

            host_output_path.mkdir(parents=True, exist_ok=True)
            binds.extend(["--bind", f"{host_output_path.resolve()}:/workspace/outputs"])

            if celltypist_cache_enabled:
                # Legacy interactive surfaces retain their shared CellTypist cache.
                # Evidence-grade control-plane workloads disable this implicit mount.
                _celltypist_host_path.mkdir(parents=True, exist_ok=True)
                binds.extend(
                    [
                        "--bind",
                        f"{_celltypist_host_path.resolve()}:/workspace/celltypist_models",
                    ]
                )

            self._binds = binds
            self._host_output_path = host_output_path

        def _read_process_line(
            self,
            deadline: float,
            cancel_event: threading.Event | None = None,
        ) -> Tuple[Optional[str], str]:
            """Read one response line without an unbounded ``readline`` call."""
            proc = self._proc
            if proc is None or proc.stdout is None:
                return None, "eof"

            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    newline = self._stdout_buffer.find(b"\n")
                    if newline >= 0:
                        raw_line = bytes(self._stdout_buffer[:newline])
                        del self._stdout_buffer[: newline + 1]
                        return raw_line.decode("utf-8", errors="replace"), "line"

                    if cancel_event is not None and cancel_event.is_set():
                        return None, "cancelled"

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, "timeout"

                    process_exited = proc.poll() is not None
                    events = selector.select(0 if process_exited else min(remaining, 0.1))
                    if not events:
                        if process_exited:
                            if self._stdout_buffer:
                                raw_line = bytes(self._stdout_buffer)
                                self._stdout_buffer.clear()
                                return raw_line.decode("utf-8", errors="replace"), "line"
                            return None, "eof"
                        continue

                    chunk = os.read(proc.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        if self._stdout_buffer:
                            raw_line = bytes(self._stdout_buffer)
                            self._stdout_buffer.clear()
                            return raw_line.decode("utf-8", errors="replace"), "line"
                        return None, "eof"
                    self._stdout_buffer.extend(chunk)

        def _invalidate_repl(self):
            """Terminate, reap, and forget the REPL so late results cannot leak."""
            proc = self._proc
            self._proc = None
            self._stdout_buffer.clear()
            if proc is None:
                return
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Container lifecycle
        # ------------------------------------------------------------------
        def start_container(self):
            self.last_start_error = None
            if self._proc:
                if self._proc.poll() is None:
                    return True  # already running
                self._invalidate_repl()
            if not ensure_sif():
                return False

            # Build command, conditionally including --nv if GPU is available
            cmd = [
                sing.require_sing_bin(),
                "exec",
            ]

            gpu_available = _nvidia_gpu_available()
            if gpu_enabled is True and not gpu_available:
                console.print(
                    "[red]GPU execution was requested but no NVIDIA GPU is accessible.[/red]"
                )
                return False
            use_gpu = gpu_available if gpu_enabled is None else gpu_enabled
            if use_gpu:
                cmd.append("--nv")
                console.print("[dim]GPU detected, enabling NVIDIA support[/dim]")
            else:
                console.print("[dim]No GPU detected, running in CPU-only mode[/dim]")

            environment_binds: list[str] = []
            python_executable = "python"
            if python_environment is not None:
                environment_binds = [
                    "--bind",
                    f"{python_environment.path}:{python_environment.path}:ro",
                ]
                python_executable = python_environment.python_executable

            container_options = [
                "--containall",
                "--cleanenv",
                "--net",
                "--network",
                "none",
                *self._binds,
                *environment_binds,
                str(SIF_PATH),
            ]
            cmd.extend([
                *container_options,
                python_executable,
                "/opt/offline_kernel.py",
                "--repl",
            ])

            retired_overrides = {
                "APPTAINERENV_MAMBA_NO_BANNER",
                "APPTAINERENV_MAMBA_NO_LOW_SPEED_LIMIT",
                "APPTAINERENV_MAMBA_ROOT_PREFIX",
                "APPTAINERENV_XDG_CACHE_HOME",
                "SINGULARITYENV_MAMBA_NO_BANNER",
                "SINGULARITYENV_MAMBA_NO_LOW_SPEED_LIMIT",
                "SINGULARITYENV_MAMBA_ROOT_PREFIX",
                "SINGULARITYENV_XDG_CACHE_HOME",
            }
            child_env = {key: value for key, value in os.environ.items() if key not in retired_overrides}
            child_env.update({
                # This SIF remains a legacy fixture, independent of the host Conda prefix.
                "SINGULARITYENV_PATH": "/usr/local/envs/rapids/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "SINGULARITYENV_PYTHONUNBUFFERED": "1",
                "SINGULARITYENV_PYTHONNOUSERSITE": "1",
                "APPTAINERENV_PYTHONUNBUFFERED": "1",
                "APPTAINERENV_PYTHONNOUSERSITE": "1",
            })
            if python_environment is not None:
                runtime_path = (
                    f"{python_environment.path}/bin:/usr/local/envs/rapids/bin:"
                    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                )
                child_env.update(
                    {
                        "SINGULARITYENV_PATH": runtime_path,
                        "APPTAINERENV_PATH": runtime_path,
                    }
                )
                if python_environment.kind is PythonEnvironmentKind.conda:
                    child_env.update(
                        {
                            "SINGULARITYENV_CONDA_PREFIX": python_environment.path,
                            "APPTAINERENV_CONDA_PREFIX": python_environment.path,
                        }
                    )
                elif python_environment.kind is PythonEnvironmentKind.venv:
                    child_env.update(
                        {
                            "SINGULARITYENV_VIRTUAL_ENV": python_environment.path,
                            "APPTAINERENV_VIRTUAL_ENV": python_environment.path,
                        }
                    )
            if celltypist_cache_enabled:
                child_env.update(
                    {
                        "SINGULARITYENV_CELLTYPIST_DATA_DIR": "/workspace/celltypist_models",
                        "SINGULARITYENV_CELLTYPIST_HOME": "/workspace/celltypist_models",
                        "SINGULARITYENV_CELLTYPIST_FOLDER": "/workspace/celltypist_models",
                    }
                )

            if python_environment is not None:
                preflight_cmd = [
                    sing.require_sing_bin(),
                    "exec",
                    *(["--nv"] if use_gpu else []),
                    *container_options,
                    python_executable,
                    "-c",
                    "import platform; print('__CARIBOU_PYTHON_VERSION__=' + platform.python_version())",
                ]
                try:
                    preflight = subprocess.run(
                        preflight_cmd,
                        capture_output=True,
                        text=True,
                        timeout=readiness_timeout,
                        check=False,
                        env=child_env,
                    )
                except Exception as exc:
                    self.last_start_error = (
                        "Selected Python environment could not be checked inside "
                        f"the Apptainer image: {exc}"
                    )
                    console.print(f"[red]{self.last_start_error}[/red]")
                    return False
                if preflight.returncode != 0:
                    detail = (preflight.stderr or preflight.stdout or "").strip()
                    self.last_start_error = (
                        "Selected Python environment is incompatible with the "
                        f"Apptainer image: {detail or 'Python failed to start.'}"
                    )
                    console.print(f"[red]{self.last_start_error}[/red]")
                    return False
                version = next(
                    (
                        line.partition("=")[2].strip()
                        for line in preflight.stdout.splitlines()
                        if line.startswith("__CARIBOU_PYTHON_VERSION__=")
                    ),
                    None,
                )
                if not version:
                    self.last_start_error = (
                        "Selected Python environment started but did not return a "
                        "valid compatibility handshake."
                    )
                    console.print(f"[red]{self.last_start_error}[/red]")
                    return False
                self.python_environment = resolved_host_environment(
                    python_environment, python_version=version
                )

            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Merge diagnostics so an unread stderr pipe cannot deadlock the REPL.
                stderr=subprocess.STDOUT,
                env=child_env,
                text=False,
                bufsize=0,
            )
            self._stdout_buffer.clear()
            ready_line, reason = self._read_process_line(time.monotonic() + readiness_timeout)
            if reason != "line" or ready_line is None or ready_line.strip() != "__REPL_READY__":
                self.last_start_error = (
                    f"Python analysis REPL failed to start ({reason}): "
                    f"{ready_line or 'no startup output'}"
                )
                console.print(
                    f"[red]REPL failed to start. Reason: {reason}; got: {ready_line or ''}[/red]"
                )
                self._invalidate_repl()
                return False
            return True

        def stop_container(self):
            self._invalidate_repl()
            return True

        # ------------------------------------------------------------------
        # Code execution
        # ------------------------------------------------------------------
        def exec_code(
            self,
            code: str,
            timeout: float = 600,
            cancel_event: threading.Event | None = None,
        ) -> Dict:
            """Execute code; timeout/cancel invalidates the stateful REPL."""
            if timeout <= 0:
                raise ValueError("timeout must be greater than zero")
            if not self._proc:
                raise SandboxReplUnavailableError("REPL not running")
            assert self._proc.stdin and self._proc.stdout

            if cancel_event is not None and cancel_event.is_set():
                self._invalidate_repl()
                return {
                    "status": "cancelled",
                    "stdout": "",
                    "stderr": "Execution cancelled; REPL invalidated.",
                    "images": [],
                }

            # Send code block + sentinel as bytes so reads can use selectors safely.
            try:
                self._proc.stdin.write(code.encode("utf-8"))
                if not code.endswith("\n"):
                    self._proc.stdin.write(b"\n")
                self._proc.stdin.write((SENTINEL + "\n").encode("utf-8"))
                self._proc.stdin.flush()
            except Exception as error:
                self._invalidate_repl()
                return {
                    "status": "error",
                    "stdout": "",
                    "stderr": f"Failed to send code to REPL: {error}",
                    "images": [],
                }

            deadline = time.monotonic() + timeout
            while True:
                line, reason = self._read_process_line(deadline, cancel_event)
                if reason in {"timeout", "cancelled", "eof"}:
                    self._invalidate_repl()
                    if reason == "timeout":
                        return {
                            "status": "timeout",
                            "stdout": "",
                            "stderr": "Execution timed out; REPL invalidated.",
                            "images": [],
                        }
                    if reason == "cancelled":
                        return {
                            "status": "cancelled",
                            "stdout": "",
                            "stderr": "Execution cancelled; REPL invalidated.",
                            "images": [],
                        }
                    return {
                        "status": "error",
                        "stdout": "",
                        "stderr": "REPL exited before returning a result.",
                        "images": [],
                    }

                assert line is not None
                try:
                    result = json.loads(line)
                    if isinstance(result, dict):
                        return result
                except json.JSONDecodeError:
                    # Non‑JSON noise; continue reading
                    continue

        # ------------------------------------------------------------------
        # Output collection helpers
        # ------------------------------------------------------------------
        def list_output_files(self) -> List[Dict]:
            """
            For Singularity, outputs live on the host; list them if available.
            """
            out_dir = self._host_output_path
            if not out_dir or not out_dir.exists():
                return []
            return [
                {"name": f.name, "size": f"{f.stat().st_size / 1e6:.2f} MB"}
                for f in out_dir.iterdir()
                if f.is_file()
            ]

        def retrieve_output_files(self, host_destination_path: Path, file_names: Optional[List[str]] = None) -> None:
            """
            For Singularity, files are already on host_output_path; this confirms location
            or copies a selected subset to another host directory if requested.
            """
            source_dir = self._host_output_path
            if not source_dir or not source_dir.exists():
                console.print("[yellow]No output directory available to retrieve from.[/yellow]")
                return

            # If the destination is the same as the source, just acknowledge
            if host_destination_path.resolve() == source_dir.resolve():
                console.print(f"[bold green]✓ Session outputs are already saved in:[/bold green] {host_destination_path}")
                return

            host_destination_path.mkdir(parents=True, exist_ok=True)
            selected = file_names or [f.name for f in source_dir.iterdir() if f.is_file()]
            for name in selected:
                src = source_dir / name
                if src.exists() and src.is_file():
                    dest = host_destination_path / name
                    dest.write_bytes(src.read_bytes())
            console.print(f"[bold green]✓ Saved selected outputs to:[/bold green] {host_destination_path}")

    _BackendManager = _SingExecBackend

    def COPY_CMD(src: str, dst: str):
        console.print("[yellow]singularity-exec mode uses bind mounts instead of docker cp.[/yellow]")
    
    return _BackendManager, None, COPY_CMD, None, None
    
    
    
