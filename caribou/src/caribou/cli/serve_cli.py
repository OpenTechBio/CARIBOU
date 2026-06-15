from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import typer

serve_app = typer.Typer(
    name="serve",
    help="Start the CARIBOU web server (API + Angular frontend).",
    no_args_is_help=False,
    invoke_without_command=True,
)


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_FRONTEND_DIR = _PACKAGE_ROOT / "frontend"


def _backend_proxy_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _write_proxy_config(host: str, port: int) -> Path:
    proxy_host = _backend_proxy_host(host)
    config = {
        "/api": {
            "target": f"http://{proxy_host}:{port}",
            "secure": False,
            "changeOrigin": True,
        },
        "/ws": {
            "target": f"ws://{proxy_host}:{port}",
            "secure": False,
            "ws": True,
            "changeOrigin": True,
        },
    }
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="caribou-proxy-",
        suffix=".json",
        delete=False,
    )
    with handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    return Path(handle.name)


def _start_frontend_dev_server(
    *,
    frontend_dir: Path,
    host: str,
    port: int,
    proxy_config: Path,
) -> subprocess.Popen:
    if not (frontend_dir / "package.json").exists():
        raise typer.BadParameter(f"Angular frontend not found at {frontend_dir}")

    return subprocess.Popen(
        [
            "npm",
            "run",
            "start",
            "--",
            "--host",
            host,
            "--port",
            str(port),
            "--proxy-config",
            str(proxy_config),
        ],
        cwd=frontend_dir,
    )


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@serve_app.callback(invoke_without_command=True)
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (development)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of uvicorn workers."),
    refresh: bool = typer.Option(False, "--refresh", help="Run Angular dev server with browser auto-refresh."),
    frontend_port: int = typer.Option(4200, "--frontend-port", help="Angular dev server port when --refresh is set."),
) -> None:
    """
    Start the CARIBOU web server.

    Open OnDemand (OOD): access at https://<ood-host>/node/<hostname>/<port>/
    The server auto-detects the OOD proxy path — no extra flags needed.

    Development (local ng serve + SSH tunnel):
      Run this on HPC, then in frontend/: ng serve --proxy-config proxy.conf.json
    """
    import uvicorn

    display_host = _display_host(host)
    typer.echo(f"Starting CARIBOU server at http://{display_host}:{port}")
    if display_host != host:
        typer.echo(f"Backend bound to {host}:{port}")
    typer.echo(f"OOD access: https://<ood-host>/node/<hostname>/{port}/")

    frontend_process = None
    proxy_config = None
    try:
        if refresh:
            proxy_config = _write_proxy_config(host, port)
            frontend_process = _start_frontend_dev_server(
                frontend_dir=_FRONTEND_DIR,
                host=host,
                port=frontend_port,
                proxy_config=proxy_config,
            )
            typer.echo(f"Angular dev server: http://{display_host}:{frontend_port}")
            if display_host != host:
                typer.echo(f"Angular dev server bound to {host}:{frontend_port}")
            typer.echo("Browser auto-refresh is enabled for frontend changes.")
            reload = True

        uvicorn.run(
            "caribou.server.main:app",
            host=host,
            port=port,
            reload=reload,
            workers=workers if not reload else 1,
            log_level="info",
        )
    except FileNotFoundError as exc:
        if exc.filename == "npm":
            typer.echo("Unable to start Angular dev server: npm was not found.")
            raise typer.Exit(1) from exc
        raise
    finally:
        if frontend_process is not None:
            _stop_process(frontend_process)
        if proxy_config is not None:
            proxy_config.unlink(missing_ok=True)
