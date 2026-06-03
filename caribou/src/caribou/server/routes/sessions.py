from __future__ import annotations

from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from caribou.server.models import (
    ArtifactRecord,
    CodeEventRecord,
    MessageRecord,
    SessionCreateRequest,
    SessionResponse,
)
from caribou.server.session_manager import session_manager


class ExtendRequest(BaseModel):
    additional_turns: int

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreateRequest) -> SessionResponse:
    return await session_manager.create_session(body)


@router.get("", response_model=List[SessionResponse])
async def list_sessions() -> List[SessionResponse]:
    return session_manager.list_sessions()


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return s.to_response()


@router.post("/{session_id}/extend", response_model=SessionResponse)
async def extend_session(session_id: str, body: ExtendRequest) -> SessionResponse:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s.config.mode.value != "auto":
        raise HTTPException(400, "Only auto sessions can be extended")
    if s.status.value != "stopped":
        raise HTTPException(400, "Session must be stopped before extending")
    if not s.live_history:
        raise HTTPException(400, "No conversation history to resume from — session may have been restored from disk after a restart")
    ok = await session_manager.extend_run(session_id, body.additional_turns)
    if not ok:
        raise HTTPException(500, "Failed to extend session")
    return s.to_response()


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    await session_manager.delete_session(session_id)


@router.get("/{session_id}/messages", response_model=List[MessageRecord])
async def get_messages(
    session_id: str, offset: int = 0, limit: int = 500
) -> List[MessageRecord]:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return s.messages[offset : offset + limit]


@router.get("/{session_id}/artifacts", response_model=List[ArtifactRecord])
async def get_artifacts(session_id: str) -> List[ArtifactRecord]:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return s.artifacts


@router.get("/{session_id}/artifacts/{artifact_id}/download")
async def download_artifact(session_id: str, artifact_id: str):
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")

    artifact = next((a for a in s.artifacts if a.id == artifact_id), None)
    if not artifact:
        raise HTTPException(404, "Artifact not found")

    path = Path(artifact.local_path)
    if not path.exists():
        raise HTTPException(404, "Artifact file not found on disk")

    return FileResponse(
        path=str(path),
        media_type=artifact.mime_type,
        filename=artifact.filename,
    )


@router.get("/{session_id}/code_events", response_model=List[CodeEventRecord])
async def get_code_events(session_id: str) -> List[CodeEventRecord]:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    return s.code_events
