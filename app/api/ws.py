import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.rate_limit import check_rate_limit
from app.errors import ApiError
from app.services.shared import read_room
from app.services.state import build_snapshot

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/rooms/{slug}")
async def room_websocket(websocket: WebSocket, slug: str) -> None:
    settings: Any = websocket.app.state.settings
    manager: Any = websocket.app.state.manager
    engine: Any = websocket.app.state.engine

    # 1. Check Origin against ALLOWED_ORIGINS (exact match): close(code=1008) BEFORE accept
    origin = websocket.headers.get("origin")
    if origin not in settings.ALLOWED_ORIGINS:
        # DECISION: Close with 1008 before accepting connection when Origin is not allowed.
        await websocket.close(code=1008)
        return

    # 2. Check room exists and is not expired: accept, then close with code 4404
    try:
        async with engine.connect() as connection:
            room = await read_room(connection, slug)
    except ApiError:
        # DECISION: Missing or expired rooms are accepted then closed with code 4404.
        await websocket.accept()
        await websocket.close(code=4404)
        return

    await websocket.accept()

    # 3. Check Redis rate limit for ws_connect (30/min per IP)
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"
    allowed, _ = await check_rate_limit(
        websocket.app.state.redis, settings, "ws_connect", client_ip
    )
    if not allowed:
        # DECISION: Close over-the-limit WebSocket connections with 1013 (Try Again Later).
        await websocket.close(code=1013)
        return

    # 4. Check per-worker connection limits: 20 per room, 5 per client IP -> close 1013
    group_id = room["id"]
    if not manager.can_connect(group_id, client_ip):
        # DECISION: Close with 1013 when per-room or per-IP socket limits are reached.
        await websocket.close(code=1013)
        return

    # 5. Register socket and send initial snapshot message
    manager.connect(group_id, client_ip, websocket)
    try:
        snapshot = await build_snapshot(engine, settings, slug)
        initial_msg = {
            "type": "snapshot",
            "version": snapshot["room"]["graph_version"],
            "state": snapshot,
        }
        await websocket.send_text(manager.format_message(json.dumps(initial_msg)))

        # 5. Message handling loop
        while True:
            data = await websocket.receive_text()
            if len(data.encode("utf-8")) > 1024:
                # DECISION: Close with 1009 if client sends any message larger than 1 KB.
                await websocket.close(code=1009)
                break
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and parsed.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                # Ignore non-ping or malformed messages
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        manager.disconnect(group_id, client_ip, websocket)
