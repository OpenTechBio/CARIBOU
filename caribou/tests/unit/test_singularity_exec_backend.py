from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from caribou.core import sandbox_management


class RecordingConsole:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> int:
        if self.closed:
            raise BrokenPipeError("stdin is closed")
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        if self.process.on_flush is not None:
            self.process.on_flush(self.process)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(
        self,
        *,
        ready: bool,
        on_flush: Callable[["FakeProcess"], None] | None,
    ) -> None:
        read_fd, self._write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = FakeStdin(self)
        self.on_flush = on_flush
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        if ready:
            self.emit(b"__REPL_READY__\n")

    def emit(self, payload: bytes) -> None:
        if self._write_fd >= 0:
            os.write(self._write_fd, payload)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15
        self._close_writer()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._close_writer()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            self.returncode = 0
            self._close_writer()
        return self.returncode

    def _close_writer(self) -> None:
        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1


class FakeSubprocess:
    PIPE = object()
    STDOUT = object()

    def __init__(
        self,
        *,
        ready: bool = True,
        on_flush: Callable[[FakeProcess], None] | None = None,
    ) -> None:
        self.ready = ready
        self.on_flush = on_flush
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.processes: list[FakeProcess] = []

    def Popen(self, command: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((command, kwargs))
        process = FakeProcess(ready=self.ready, on_flush=self.on_flush)
        self.processes.append(process)
        return process


def _configure_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_subprocess: FakeSubprocess,
    *,
    image_contents: bytes = b"pinned legacy fixture",
    expected_digest: str | None = None,
    readiness_timeout: float = 0.2,
    gpu_enabled: bool | None = None,
    celltypist_cache_enabled: bool = True,
):
    import caribou.sandbox.benchmarking_sandbox_management_singularity as sing

    image_path = tmp_path / "legacy-sandbox.sif"
    image_path.write_bytes(image_contents)
    actual_digest = hashlib.sha256(image_contents).hexdigest()
    monkeypatch.setattr(sandbox_management, "CARIBOU_HOME", tmp_path / "caribou-home")
    monkeypatch.setattr(sandbox_management, "_nvidia_gpu_available", lambda: False)
    monkeypatch.setattr(sing, "require_sing_bin", lambda: "apptainer")

    console = RecordingConsole()
    manager_class, _, _, _, _ = sandbox_management.init_singularity_exec(
        str(tmp_path),
        "/workspace/dataset.h5ad",
        fake_subprocess,
        console,
        sif_path=image_path,
        sif_sha256=expected_digest or f"sha256:{actual_digest}",
        no_pull=True,
        readiness_timeout=readiness_timeout,
        gpu_enabled=gpu_enabled,
        celltypist_cache_enabled=celltypist_cache_enabled,
    )
    return manager_class, console, image_path, actual_digest


def test_singularity_exec_uses_pinned_image_without_persisted_run_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = b'{"status":"ok","stdout":"done\\n","stderr":"","images":[]}\n'
    fake_subprocess = FakeSubprocess(on_flush=lambda process: process.emit(result))
    manager_class, _, image_path, actual_digest = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
    )
    monkeypatch.setenv("SINGULARITYENV_MAMBA_ROOT_PREFIX", "/outputs/.mamba")
    monkeypatch.setenv("SINGULARITYENV_XDG_CACHE_HOME", "/outputs/.cache")
    monkeypatch.setenv("APPTAINERENV_MAMBA_ROOT_PREFIX", "/outputs/.mamba")

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    backend = manager_class()
    input_path = tmp_path / "input.h5ad"
    input_path.write_bytes(b"input")
    backend.set_data([(input_path, "/workspace/dataset.h5ad")], output_dir)

    assert backend.image_path == image_path.resolve()
    assert backend.image_sha256 == actual_digest
    assert backend.image_no_pull is True
    assert backend.start_container() is True
    assert backend.exec_code("print('done')", timeout=1)["status"] == "ok"

    command, kwargs = fake_subprocess.calls[0]
    assert str(image_path.resolve()) in command
    assert command[command.index("--network") + 1] == "none"
    assert f"{input_path.resolve()}:/workspace/dataset.h5ad:ro" in command
    assert f"{output_dir.resolve()}:/workspace/outputs" in command
    assert kwargs["stderr"] is fake_subprocess.STDOUT
    assert kwargs["text"] is False
    assert kwargs["bufsize"] == 0
    child_env = kwargs["env"]
    assert "SINGULARITYENV_MAMBA_ROOT_PREFIX" not in child_env
    assert "SINGULARITYENV_XDG_CACHE_HOME" not in child_env
    assert "APPTAINERENV_MAMBA_ROOT_PREFIX" not in child_env
    assert child_env["SINGULARITYENV_CELLTYPIST_HOME"] == (
        "/workspace/celltypist_models"
    )
    assert child_env["SINGULARITYENV_CELLTYPIST_FOLDER"] == (
        "/workspace/celltypist_models"
    )
    assert (tmp_path / "caribou-home" / "celltypist_models").is_dir()
    assert not (output_dir / ".cache").exists()
    assert not (output_dir / ".mamba").exists()

    process = fake_subprocess.processes[0]
    assert b"<<<EOF>>>\n" in process.stdin.writes
    assert backend.stop_container() is True
    assert process.wait_calls


def test_singularity_exec_can_disable_implicit_celltypist_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess()
    manager_class, _, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
        celltypist_cache_enabled=False,
    )
    output_dir = tmp_path / "outputs"
    backend = manager_class()
    backend.set_data([], output_dir)

    assert backend.start_container() is True
    command, kwargs = fake_subprocess.calls[0]
    assert not any("celltypist_models" in argument for argument in command)
    assert not any("CELLTYPIST" in key for key in kwargs["env"])
    assert not (tmp_path / "caribou-home" / "celltypist_models").exists()


def test_singularity_exec_rejects_hash_mismatch_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess()
    manager_class, console, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
        expected_digest="0" * 64,
    )

    backend = manager_class()
    assert backend.start_container() is False
    assert fake_subprocess.calls == []
    assert any("SHA-256 mismatch" in message for message in console.messages)


def test_singularity_exec_timeout_terminates_reaps_and_invalidates_repl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess()
    manager_class, _, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
    )
    backend = manager_class()
    assert backend.start_container() is True
    process = fake_subprocess.processes[0]

    result = backend.exec_code("while True: pass", timeout=0.02)

    assert result["status"] == "timeout"
    assert process.terminate_calls == 1
    assert process.wait_calls
    assert process.stdin.closed
    assert process.stdout.closed
    assert backend._proc is None
    with pytest.raises(RuntimeError, match="REPL not running"):
        backend.exec_code("print('must not consume a stale result')")


def test_singularity_exec_cancellation_terminates_reaps_and_invalidates_repl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess()
    manager_class, _, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
    )
    backend = manager_class()
    assert backend.start_container() is True
    process = fake_subprocess.processes[0]
    cancel_event = threading.Event()
    timer = threading.Timer(0.02, cancel_event.set)
    timer.start()
    try:
        result = backend.exec_code(
            "while True: pass", timeout=1, cancel_event=cancel_event
        )
    finally:
        timer.join()

    assert result["status"] == "cancelled"
    assert process.terminate_calls == 1
    assert process.wait_calls
    assert backend._proc is None


def test_singularity_exec_readiness_timeout_reaps_failed_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess(ready=False)
    manager_class, console, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
        readiness_timeout=0.02,
    )
    backend = manager_class()

    assert backend.start_container() is False
    process = fake_subprocess.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls
    assert backend._proc is None
    assert any("Reason: timeout" in message for message in console.messages)


def test_singularity_exec_validates_new_image_options(
    tmp_path: Path,
) -> None:
    console = RecordingConsole()
    fake_subprocess = FakeSubprocess()

    with pytest.raises(ValueError, match="cannot be used together"):
        sandbox_management.init_singularity_exec(
            str(tmp_path),
            "/workspace/dataset.h5ad",
            fake_subprocess,
            console,
            force_refresh=True,
            no_pull=True,
        )
    with pytest.raises(ValueError, match="64-character"):
        sandbox_management.init_singularity_exec(
            str(tmp_path),
            "/workspace/dataset.h5ad",
            fake_subprocess,
            console,
            sif_sha256="not-a-digest",
        )
    with pytest.raises(ValueError, match="explicit sif_path requires"):
        sandbox_management.init_singularity_exec(
            str(tmp_path),
            "/workspace/dataset.h5ad",
            fake_subprocess,
            console,
            sif_path=tmp_path / "unpinned.sif",
        )


def test_singularity_exec_rejects_unavailable_requested_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_subprocess = FakeSubprocess()
    manager_class, console, _, _ = _configure_backend(
        tmp_path,
        monkeypatch,
        fake_subprocess,
        gpu_enabled=True,
    )

    assert manager_class().start_container() is False
    assert fake_subprocess.calls == []
    assert any("no NVIDIA GPU" in message for message in console.messages)
