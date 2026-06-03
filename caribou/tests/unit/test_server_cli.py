import os

from caribou.cli.server_cli import (
    ProcessInfo,
    _find_server_processes,
    _is_caribou_server_process,
)


def test_matches_caribou_serve_processes():
    assert _is_caribou_server_process("/env/bin/caribou serve --port 8000")
    assert _is_caribou_server_process("caribou serve")
    assert _is_caribou_server_process("python -m caribou.cli.main serve --port 8000")


def test_matches_uvicorn_caribou_server_processes():
    assert _is_caribou_server_process("uvicorn caribou.server.main:app --port 8000")
    assert _is_caribou_server_process("python -m uvicorn caribou.server.main:app")


def test_does_not_match_unrelated_processes():
    assert not _is_caribou_server_process("uvicorn other.app:app --port 8000")
    assert not _is_caribou_server_process("python -m pytest caribou/tests")
    assert not _is_caribou_server_process("caribou run")


def test_find_server_processes_excludes_current_pid():
    current = os.getpid()
    processes = [
        ProcessInfo(pid=current, command="caribou serve --port 8000"),
        ProcessInfo(pid=current + 1, command="caribou serve --port 8001"),
        ProcessInfo(pid=current + 2, command="uvicorn other.app:app"),
    ]

    assert _find_server_processes(processes) == [
        ProcessInfo(pid=current + 1, command="caribou serve --port 8001")
    ]
