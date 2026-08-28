import json
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import typer

from caribou.cli.serve_cli import (
    _announce_control_access_token,
    _backend_proxy_host,
    _display_host,
    _port_in_use,
    _resolve_serve_port,
    _start_frontend_dev_server,
    _stop_process,
    _write_proxy_config,
    serve,
)
from caribou.core.control_access import ControlAccessToken


def test_backend_proxy_host_uses_loopback_for_bind_all_hosts():
    assert _backend_proxy_host("0.0.0.0") == "127.0.0.1"
    assert _backend_proxy_host("::") == "127.0.0.1"
    assert _backend_proxy_host("localhost") == "localhost"


def test_display_host_uses_openable_loopback_for_bind_all_hosts():
    assert _display_host("0.0.0.0") == "127.0.0.1"
    assert _display_host("::") == "127.0.0.1"
    assert _display_host("localhost") == "localhost"


def test_write_proxy_config_points_api_and_ws_to_backend_port():
    path = _write_proxy_config("0.0.0.0", 9000)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)

    assert config["/api"]["target"] == "http://127.0.0.1:9000"
    assert config["/ws"]["target"] == "ws://127.0.0.1:9000"
    assert config["/ws"]["ws"] is True


def test_start_frontend_dev_server_runs_angular_with_proxy_config(tmp_path):
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "package.json").write_text("{}", encoding="utf-8")
    proxy_config = tmp_path / "proxy.json"
    proxy_config.write_text("{}", encoding="utf-8")

    with patch("caribou.cli.serve_cli.subprocess.Popen") as popen:
        _start_frontend_dev_server(
            frontend_dir=frontend_dir,
            host="0.0.0.0",
            port=4201,
            proxy_config=proxy_config,
        )

    popen.assert_called_once_with(
        [
            "npm",
            "run",
            "start",
            "--",
            "--host",
            "0.0.0.0",
            "--port",
            "4201",
            "--proxy-config",
            str(proxy_config),
        ],
        cwd=frontend_dir,
    )


def test_start_frontend_dev_server_requires_frontend_package(tmp_path):
    with pytest.raises(typer.BadParameter):
        _start_frontend_dev_server(
            frontend_dir=tmp_path,
            host="0.0.0.0",
            port=4200,
            proxy_config=Path("proxy.json"),
        )


def _bind_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_port_in_use_reports_a_bound_port():
    port = _bind_ephemeral_port()
    # Re-bind the same port to simulate another process already listening.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        assert _port_in_use("127.0.0.1", port) is True
    finally:
        blocker.close()


def test_resolve_serve_port_keeps_a_free_port(monkeypatch):
    port = _bind_ephemeral_port()
    monkeypatch.setattr("caribou.cli.serve_cli._port_in_use", lambda h, p: False)
    assert _resolve_serve_port("127.0.0.1", port) == port


def test_resolve_serve_port_prompts_interactively_for_an_alternative(monkeypatch):
    port = _bind_ephemeral_port()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("caribou.cli.serve_cli.IntPrompt.ask", Mock(return_value=port + 1))
    monkeypatch.setattr(
        "caribou.cli.serve_cli._port_in_use",
        lambda h, p: p == port,
    )
    assert _resolve_serve_port("127.0.0.1", port) == port + 1


def test_resolve_serve_port_fails_loudly_without_a_tty(monkeypatch):
    port = _bind_ephemeral_port()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "caribou.cli.serve_cli._port_in_use", lambda h, p: True
    )
    with pytest.raises(typer.Exit):
        _resolve_serve_port("127.0.0.1", port)


def test_stop_process_terminates_then_kills_if_needed():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("wait", 5), None]

    _stop_process(process)

    process.terminate.assert_called_once()
    process.kill.assert_called_once()


def test_serve_announcement_prints_exact_retrievable_token(capsys, tmp_path):
    env_file = tmp_path / ".env"
    _announce_control_access_token(
        ControlAccessToken(
            value="control-token-visible-at-startup",
            source="generated",
            env_file=env_file,
        )
    )

    output = capsys.readouterr().out
    assert "control-token-visible-at-startup" in output
    assert "caribou config get-control-token" in output
    assert str(env_file) in output


def test_serve_resolves_and_displays_control_token_before_uvicorn(capsys):
    resolved = ControlAccessToken(
        value="serve-control-token",
        source="environment",
    )
    with (
        patch("caribou.cli.serve_cli._ensure_frontend_built"),
        patch("caribou.cli.serve_cli._resolve_serve_port", side_effect=lambda h, p: p),
        patch(
            "caribou.cli.serve_cli.resolve_control_access_token",
            return_value=resolved,
        ) as resolve,
        patch("uvicorn.run") as uvicorn_run,
    ):
        serve(
            host="127.0.0.1",
            port=8765,
            reload=False,
            workers=1,
            refresh=False,
            frontend_port=4200,
        )

    resolve.assert_called_once()
    uvicorn_run.assert_called_once()
    output = capsys.readouterr().out
    assert "serve-control-token" in output
    assert "Experiment access token" in output
