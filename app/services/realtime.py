import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.redis_client import RedisClient
from app.services import state

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self, settings: Any):
        self.settings = settings
        self.rooms: dict[int, set[WebSocket]] = defaultdict(set)
        self.ip_sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self.socket_ips: dict[WebSocket, str] = {}

    def can_connect(self, group_id: int, client_ip: str) -> bool:
        # DECISION: Per-worker caps: maximum 20 sockets per room and 5 sockets per client IP.
        room, ip_set = self.rooms.get(group_id, ()), self.ip_sockets.get(client_ip, ())
        return len(room) < 20 and len(ip_set) < 5

    def connect(self, group_id: int, client_ip: str, ws: WebSocket) -> None:
        self.rooms[group_id].add(ws)
        self.ip_sockets[client_ip].add(ws)
        self.socket_ips[ws] = client_ip

    def disconnect(self, group_id: int, client_ip: str, ws: WebSocket) -> None:
        room = self.rooms.get(group_id)
        if room is not None:
            room.discard(ws)
            if not room:
                del self.rooms[group_id]
        ip_set = self.ip_sockets.get(client_ip)
        if ip_set is not None:
            ip_set.discard(ws)
            if not ip_set:
                del self.ip_sockets[client_ip]
        self.socket_ips.pop(ws, None)

    def format_message(self, text_payload: str) -> str:
        # DECISION: Add worker_pid at socket send time only when EXPOSE_WORKER_PID is true.
        if self.settings.EXPOSE_WORKER_PID:
            try:
                data = json.loads(text_payload)
                if isinstance(data, dict):
                    data["worker_pid"] = os.getpid()
                    return json.dumps(data)
            except Exception:
                pass
        return text_payload

    async def broadcast(self, group_id: int, text_payload: str) -> None:
        sockets = list(self.rooms.get(group_id, set()))
        if not sockets:
            return

        formatted = self.format_message(text_payload)

        async def _send(ws: WebSocket):
            try:
                await asyncio.wait_for(ws.send_text(formatted), timeout=2.0)
                return ws, True
            except Exception:
                try:
                    await ws.close()
                except Exception:
                    pass
                return ws, False

        results = await asyncio.gather(*(_send(ws) for ws in sockets), return_exceptions=True)
        for item in results:
            if isinstance(item, tuple) and not item[1]:
                ws = item[0]
                self.disconnect(group_id, self.socket_ips.get(ws, ""), ws)


async def publish(
    redis_client: RedisClient | None, manager: ConnectionManager, group_id: int, payload: str
) -> None:
    published = False
    if redis_client is not None:
        try:
            published = await redis_client.publish(f"splitsync:room:{group_id}", payload)
        except Exception as exc:
            logger.warning("Redis publish error for room %s: %s", group_id, exc)
            published = False

    if not published:
        # DECISION: Fall back to local worker broadcast immediately if Redis publish fails.
        logger.warning("Falling back to local broadcast for room %s", group_id)
        await manager.broadcast(group_id, payload)


async def redis_listener(redis_client: RedisClient | None, manager: ConnectionManager) -> None:
    backoff = 1.0
    while True:
        try:
            if redis_client is None:
                raise ConnectionError("Redis is not configured")
            pubsub = redis_client.pubsub()
            await pubsub.psubscribe("splitsync:room:*")
            logger.info("Subscribed to Redis pattern splitsync:room:*")
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") == "pmessage":
                    channel = message.get("channel", "")
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8")
                    parts = channel.split(":")
                    if (
                        len(parts) == 3
                        and parts[0] == "splitsync"
                        and parts[1] == "room"
                        and parts[2].isdigit()
                    ):
                        group_id = int(parts[2])
                        data = message.get("data", "")
                        if isinstance(data, bytes):
                            data = data.decode("utf-8")
                        await manager.broadcast(group_id, data)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Redis listener error: %s; reconnecting in %.1fs", exc, backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2.0, 30.0)


async def publish_snapshot_task(app: Any, slug: str) -> None:
    try:
        engine = app.state.engine
        settings = app.state.settings
        redis_client = getattr(app.state, "redis", None)
        manager = getattr(app.state, "manager", None)
        if manager is None:
            return

        group_id, snapshot = await state.load_snapshot(engine, settings, slug)
        version = snapshot["room"]["graph_version"]
        payload = json.dumps({"type": "snapshot", "version": version, "state": snapshot})
        await publish(redis_client, manager, group_id, payload)
    except Exception as exc:
        # DECISION: Catch background publish errors and log without failing HTTP requests.
        logger.warning("Background publish failed for room %s: %s", slug, exc)
