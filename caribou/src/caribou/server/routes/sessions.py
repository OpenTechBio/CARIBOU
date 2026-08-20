from __future__ import annotations

import json
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from caribou.core.python_environments import PythonEnvironmentError
from caribou.execution.evaluation import EvaluationContextTooLarge
from caribou.server.models import (
    ArtifactRecord,
    CodeEventRecord,
    EvaluationResult,
    EvaluatorModelState,
    EvaluatorModelUpdateRequest,
    MessageRecord,
    SessionCreateRequest,
    SessionForkRequest,
    SessionResumeRequest,
    SessionResponse,
)
from caribou.server.session_manager import session_manager


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreateRequest) -> SessionResponse:
    try:
        return await session_manager.create_session(body)
    except PythonEnvironmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _lifecycle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    if isinstance(exc, PythonEnvironmentError):
        return HTTPException(400, str(exc))
    return HTTPException(409, str(exc))


@router.post("/{session_id}/resume", response_model=SessionResponse, status_code=202)
async def resume_session(
    session_id: str, body: SessionResumeRequest
) -> SessionResponse:
    try:
        return await session_manager.resume_session(session_id, body)
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/{session_id}/fork", response_model=SessionResponse, status_code=202)
async def fork_session(session_id: str, body: SessionForkRequest) -> SessionResponse:
    try:
        return await session_manager.fork_session(session_id, body)
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post(
    "/{session_id}/recovery/retry", response_model=SessionResponse, status_code=202
)
async def retry_recovery(
    session_id: str, body: SessionResumeRequest
) -> SessionResponse:
    try:
        return await session_manager.retry_recovery(session_id, body)
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.post("/{session_id}/recovery/accept-partial", response_model=SessionResponse)
async def accept_partial_recovery(session_id: str) -> SessionResponse:
    try:
        return await session_manager.accept_partial_recovery(session_id)
    except (KeyError, ValueError) as exc:
        raise _lifecycle_error(exc) from exc


@router.get("", response_model=List[SessionResponse])
async def list_sessions() -> List[SessionResponse]:
    return session_manager.list_sessions()


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
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


@router.get("/{session_id}/notebook")
async def download_notebook(session_id: str):
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")

    from caribou.core.io_helpers import chat_history_to_notebook

    history = [
        {"role": message.role, "content": message.content}
        for message in s.messages
        if message.role in ("user", "assistant")
    ]
    notebook = chat_history_to_notebook(history)
    filename = f"caribou-session-{session_id[:8]}.ipynb"
    return Response(
        content=json.dumps(notebook, indent=2),
        media_type="application/x-ipynb+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@router.get("/{session_id}/memory")
async def get_memory_state(session_id: str) -> dict:
    """Return the current memory state and context breakdown of the session."""
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    state = session_manager.get_context_breakdown(session_id)
    return state


@router.post("/{session_id}/evaluate", response_model=EvaluationResult)
async def evaluate_session(session_id: str) -> EvaluationResult:
    """Send this session's full transcript to an evaluator agent for review."""
    s = session_manager.get_session(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if s.evaluator_llm_client is None or s.agent_system is None:
        raise HTTPException(
            400,
            "Session is not running — start (or restart) the run before evaluating it.",
        )
    try:
        return await session_manager.evaluate_session(session_id)
    except EvaluationContextTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{session_id}/evaluator-model", response_model=EvaluatorModelState)
async def get_evaluator_model(session_id: str) -> EvaluatorModelState:
    try:
        return session_manager.get_evaluator_model(session_id)
    except KeyError as exc:
        raise HTTPException(404, "Session not found") from exc


@router.patch("/{session_id}/evaluator-model", response_model=EvaluatorModelState)
async def update_evaluator_model(
    session_id: str, body: EvaluatorModelUpdateRequest
) -> EvaluatorModelState:
    try:
        return await session_manager.update_evaluator_model(session_id, body)
    except KeyError as exc:
        raise HTTPException(404, "Session not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
