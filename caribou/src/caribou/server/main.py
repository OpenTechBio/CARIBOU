"""
CARIBOU web server entry point.

Serves:
  - REST API at /api/*
  - WebSocket at /ws/sessions/{id}
  - Angular SPA at /* (static files packaged in caribou/frontend/browser/)

OOD note: Open OnDemand's Apache proxy forwards /node/<host>/<port>/foo to the
server WITHOUT stripping the prefix. OodPathMiddleware auto-detects this pattern
from every incoming request path — no flags or env vars needed. Works for both
direct access (localhost:8000) and OOD proxied access.
"""
from __future__ import annotations

import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from caribou.server.routes.config import router as config_router
from caribou.server.routes.sessions import router as sessions_router
from caribou.server.routes.datasets import router as datasets_router
from caribou.server.routes.experiments import (
    control_http_exception_response,
    request_validation_error_response,
    router as experiments_router,
)
from caribou.server.routes.websocket import router as ws_router
from caribou.server.ollama_service import shutdown_owned_ollama
from caribou.server.session_manager import session_manager
from caribou.server.routes.presets import router as presets_router

_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_FRONTEND_DIST = _PACKAGE_DIR / "frontend" / "browser"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Matches Open OnDemand node proxy prefix: /node/<hostname>/<port>
_OOD_PREFIX_RE = re.compile(r"^/node/[^/]+/\d+")


# ---------------------------------------------------------------------------
# OOD path-stripping middleware
# ---------------------------------------------------------------------------

class OodPathMiddleware:
    """
    Auto-detects and strips the Open OnDemand node-proxy prefix from every
    request. Pattern: /node/<hostname>/<port>/...

    Works for both OOD-proxied and direct access — direct requests never have
    a /node/... prefix so nothing is stripped.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            m = _OOD_PREFIX_RE.match(path)
            if m:
                remainder = path[m.end():]
                if remainder == "" and scope["type"] == "http":
                    # Exact prefix hit with no trailing slash — redirect to add it.
                    # Without this, <base href="./"> resolves one level too high.
                    location = (path + "/").encode()
                    await send({"type": "http.response.start", "status": 301,
                                "headers": [[b"location", location], [b"content-length", b"0"]]})
                    await send({"type": "http.response.body", "body": b""})
                    return
                stripped = remainder or "/"
                scope = dict(scope)
                scope["path"] = stripped
                scope["raw_path"] = stripped.encode()
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await session_manager.shutdown_all()
    shutdown_owned_ollama()


app = FastAPI(
    title="CARIBOU Server",
    description="Web API for CARIBOU multi-agent LLM framework",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, error: RequestValidationError
):
    if _is_control_path(request.url.path):
        return request_validation_error_response(error)
    return await request_validation_exception_handler(request, error)


@app.exception_handler(HTTPException)
async def control_http_exception_handler(request: Request, error: HTTPException):
    if _is_control_path(request.url.path):
        return control_http_exception_response(error)
    return await http_exception_handler(request, error)


def _is_control_path(path: str) -> bool:
    """Match the control namespace without also matching similar SPA paths."""

    return path == "/api/control" or path.startswith("/api/control/")


app.add_middleware(OodPathMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(config_router)
app.include_router(sessions_router)
app.include_router(datasets_router)
app.include_router(experiments_router)
app.include_router(presets_router)
app.include_router(ws_router)


# ---------------------------------------------------------------------------
# Angular SPA static file serving
# ---------------------------------------------------------------------------

if _FRONTEND_DIST.exists():

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if _is_control_path("/" + full_path.lstrip("/")):
            return control_http_exception_response(
                HTTPException(status_code=404, detail="not found")
            )
        candidate = _FRONTEND_DIST / full_path
        if candidate.exists() and candidate.is_file():
            mime, _ = mimetypes.guess_type(str(candidate))
            return FileResponse(str(candidate), media_type=mime or "application/octet-stream")
        index = _FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(str(index), media_type="text/html")
        return {"detail": "Frontend not built — run 'ng build' in frontend/."}
