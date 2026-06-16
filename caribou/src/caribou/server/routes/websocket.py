"""
WebSocket endpoint for real-time session streaming.

Protocol:
  Client → Server:
    { "type": "run",          "content": "<initial prompt>" }
    { "type": "user_message", "content": "<next turn>"      }
    { "type": "stop"                                         }
    { "type": "ping"                                         }

  Server → Client:
    All events from the session event log (see models.make_event).
    Existing events are replayed on connect; new events are streamed live.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from caribou.server.models import SessionStatus
from caribou.server.session_manager import session_manager

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/sessions/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()

    session = session_manager.get_session(session_id)
    if not session:
        await websocket.send_json({"type": "error", "data": {"code": "NOT_FOUND", "message": "Session not found", "fatal": True}})
        await websocket.close(code=4004)
        return

    # Send full event history so the client can rebuild state on reconnect
    for event in session.events:
        try:
            await websocket.send_json(event)
        except Exception:
            return

    # Stream new events as they arrive; also handle inbound messages
    send_task = asyncio.create_task(_stream_events(websocket, session))
    recv_task = asyncio.create_task(_receive_messages(websocket, session))

    try:
        done, pending = await asyncio.wait(
            {send_task, recv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        send_task.cancel()
        recv_task.cancel()
    except Exception:
        pass


async def _stream_events(websocket: WebSocket, session) -> None:
    """Wait for new events and forward them to the WebSocket."""
    cursor = len(session.events)
    while True:
        async with session.event_condition:
            while cursor >= len(session.events):
                if session.status in (SessionStatus.stopped, SessionStatus.error):
                    return
                await session.event_condition.wait()

            while cursor < len(session.events):
                event = session.events[cursor]
                cursor += 1
                try:
                    await websocket.send_json(event)
                except Exception:
                    return

        if session.status in (SessionStatus.stopped, SessionStatus.error):
            return


async def _receive_messages(websocket: WebSocket, session) -> None:
    """Handle inbound WebSocket messages from the client."""
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")

        if msg_type == "ping":
            await websocket.send_json({
                "type": "pong",
                "session_id": session.id,
                "turn": 0,
                "timestamp": datetime.utcnow().isoformat(),
                "data": {},
            })

        elif msg_type == "run":
            content = msg.get("content", "")
            if not content:
                continue
            started = await session_manager.start_run(session.id, content)
            if not started and session.config.mode.value == "interactive":
                await session_manager.send_user_message(session.id, content)

        elif msg_type == "user_message":
            content = msg.get("content", "")
            if content:
                await session_manager.send_user_message(session.id, content)

        elif msg_type == "stop":
            await session_manager.stop_session(session.id)
