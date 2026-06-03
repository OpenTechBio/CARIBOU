from __future__ import annotations

import typer

serve_app = typer.Typer(
    name="serve",
    help="Start the CARIBOU web server (API + Angular frontend).",
    no_args_is_help=False,
    invoke_without_command=True,
)


@serve_app.callback(invoke_without_command=True)
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to."),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (development)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of uvicorn workers."),
) -> None:
    """
    Start the CARIBOU web server.

    Open OnDemand (OOD): access at https://<ood-host>/node/<hostname>/<port>/
    The server auto-detects the OOD proxy path — no extra flags needed.

    Development (local ng serve + SSH tunnel):
      Run this on HPC, then in frontend/: ng serve --proxy-config proxy.conf.json
    """
    import uvicorn

    typer.echo(f"Starting CARIBOU server at http://{host}:{port}")
    typer.echo(f"OOD access: https://<ood-host>/node/<hostname>/{port}/")

    uvicorn.run(
        "caribou.server.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
        log_level="info",
    )
