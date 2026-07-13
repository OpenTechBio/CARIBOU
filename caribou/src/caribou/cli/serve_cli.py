from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm

serve_app = typer.Typer(
    name="serve",
    help="Start the CARIBOU web server (API + Angular frontend).",
    no_args_is_help=False,
    invoke_without_command=True,
)


_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_FRONTEND_DIR = _PACKAGE_ROOT / "frontend"
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "browser"

# Files/dirs that, when newer than the built bundle, indicate a rebuild is warranted.
_FRONTEND_SRC_GLOBS = ("src/**/*",)
_FRONTEND_ROOT_FILES = ("angular.json", "package.json", "package-lock.json",
                        "tsconfig.json", "tsconfig.app.json", "tsconfig.spec.json")

_console = Console()


def _iter_frontend_sources(frontend_dir: Path):
    for pattern in _FRONTEND_SRC_GLOBS:
        yield from (p for p in frontend_dir.glob(pattern) if p.is_file())
    for name in _FRONTEND_ROOT_FILES:
        candidate = frontend_dir / name
        if candidate.is_file():
            yield candidate


def _frontend_build_state(frontend_dir: Path, dist_dir: Path) -> tuple[str, Path | None]:
    """
    Returns (state, newest_source_path):
      - "missing"  → no built index.html
      - "stale"    → sources newer than built bundle
      - "fresh"    → build is up to date
    """
    index = dist_dir / "index.html"
    if not index.exists():
        return "missing", None
    build_mtime = index.stat().st_mtime
    newest: Path | None = None
    newest_mtime = 0.0
    for src in _iter_frontend_sources(frontend_dir):
        m = src.stat().st_mtime
        if m > newest_mtime:
            newest_mtime = m
            newest = src
    if newest is not None and newest_mtime > build_mtime:
        return "stale", newest
    return "fresh", None


def _run_npm(frontend_dir: Path, args: list[str], action: str) -> None:
    _console.print(f"[cyan]Running `npm {' '.join(args)}` in {frontend_dir}…[/cyan]")
    try:
        subprocess.run(["npm", *args], cwd=frontend_dir, check=True)
    except FileNotFoundError as exc:
        _console.print(
            "[red]npm was not found on PATH. Install Node.js 18+ and retry, "
            "or run the build manually.[/red]"
        )
        raise typer.Exit(1) from exc
    except subprocess.CalledProcessError as exc:
        _console.print(f"[red]{action} failed (exit {exc.returncode}).[/red]")
        raise typer.Exit(exc.returncode) from exc


def _ensure_frontend_built(frontend_dir: Path, dist_dir: Path) -> None:
    """
    Ask the user whether to (re)build the Angular frontend when the bundle is
    missing or older than the sources. No-op if there's no frontend/ directory
    (e.g. wheel install where the bundle is already packaged).
    """
    if not (frontend_dir / "package.json").exists():
        return  # nothing to build from source; assume prebuilt bundle ships with the package

    state, newest = _frontend_build_state(frontend_dir, dist_dir)
    if state == "fresh":
        return

    if state == "missing":
        _console.print("[yellow]No built frontend bundle found at "
                       f"{dist_dir}.[/yellow]")
        default_yes = True
    else:  # stale
        _console.print(f"[yellow]Frontend sources have changed since the last build "
                       f"(newest: {newest.relative_to(frontend_dir) if newest else '?'}).[/yellow]")
        default_yes = True

    if not Confirm.ask("Rebuild the Angular frontend now?", default=default_yes):
        if state == "missing":
            _console.print("[yellow]Continuing without a built frontend — the web UI "
                           "will return 404 at /. API endpoints under /api still work.[/yellow]")
        else:
            _console.print("[yellow]Continuing with the existing (stale) bundle.[/yellow]")
        return

    if not (frontend_dir / "node_modules").exists():
        _console.print("[cyan]node_modules missing — running `npm install` first.[/cyan]")
        _run_npm(frontend_dir, ["install"], "npm install")

    _run_npm(frontend_dir, ["run", "build"], "npm run build")
    _console.print("[green]Frontend build complete.[/green]")


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

    # In --refresh mode Angular serves its own bundle from ng serve, so a
    # prebuilt dist is not required. Otherwise, offer to (re)build when the
    # bundle is missing or older than the source tree.
    if not refresh:
        _ensure_frontend_built(_FRONTEND_DIR, _FRONTEND_DIST)

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
