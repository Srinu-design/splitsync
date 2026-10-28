import asyncio
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets
import websockets.exceptions

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def ws_server(migrated_database):
    url, _ = migrated_database
    port = find_free_port()
    # DECISION: Real uvicorn server fixture runs on a free loopback port with the real Redis pool.
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://localhost:6380/0",
        ALLOWED_ORIGINS=["http://localhost:8000"],
        RATE_LIMIT_ENABLED=False,
        DB_POOL_SIZE=12,
        DB_MAX_OVERFLOW=0,
    )
    application = create_app(settings)
    config = uvicorn.Config(
        application, host="127.0.0.1", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_websocket_lifecycle_and_redis_fanout(ws_server):
    http_url, ws_url = ws_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        created = await http_client.post("/api/rooms", json={"kind": "DEMO"})
        assert created.status_code == 201
        slug = created.json()["slug"]

        # 1. Snapshot on connect with room version
        async with websockets.connect(
            f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
        ) as ws:
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert first_msg["type"] == "snapshot"
            assert first_msg["version"] == 9
            assert first_msg["state"]["room"]["slug"] == slug

            # 2. Higher version snapshot arrives within 2s after POST expense
            state_res = await http_client.get(f"/api/rooms/{slug}/state")
            members = state_res.json()["members"]
            aarav_id = next(m["id"] for m in members if m["name"] == "Aarav")

            exp_res = await http_client.post(
                f"/api/rooms/{slug}/expenses",
                headers={"Idempotency-Key": "ws-test-exp-0001"},
                json={
                    "title": "Taxi",
                    "paid_by": aarav_id,
                    "amount": "100",
                    "split": {"type": "EQUAL", "members": [{"id": m["id"]} for m in members]},
                },
            )
            assert exp_res.status_code == 201

            second_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert second_msg["type"] == "snapshot"
            assert second_msg["version"] == 10


async def test_websocket_origin_check(ws_server):
    http_url, ws_url = ws_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        created = await http_client.post("/api/rooms", json={"kind": "DEMO"})
        slug = created.json()["slug"]

    # Wrong Origin -> closed 1008 before accept
    # DECISION: Origin rejections before accept produce an HTTP 403 or close code 1008.
    with pytest.raises(
        (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed)
    ) as exc_info:
        async with websockets.connect(f"{ws_url}/ws/rooms/{slug}", origin="http://evil.com"):
            pass
    if isinstance(exc_info.value, websockets.exceptions.InvalidStatus):
        assert exc_info.value.response.status_code == 403
    else:
        assert exc_info.value.code == 1008


async def test_websocket_unknown_slug_closes_4404(ws_server):
    _, ws_url = ws_server
    # Unknown slug -> accept, then close 4404
    with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
        async with websockets.connect(
            f"{ws_url}/ws/rooms/unknown00", origin="http://localhost:8000"
        ) as ws:
            await ws.recv()
    code = exc_info.value.rcvd.code if exc_info.value.rcvd else exc_info.value.code
    assert code == 4404


async def test_websocket_ping_pong_and_payload_limit(ws_server):
    http_url, ws_url = ws_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        created = await http_client.post("/api/rooms", json={"kind": "DEMO"})
        slug = created.json()["slug"]

    # Ping -> pong
    async with websockets.connect(
        f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
    ) as ws:
        await ws.recv()  # consume initial snapshot
        await ws.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
        assert pong == {"type": "pong"}

        # >1 KB message -> close 1009
        await ws.send("x" * 1025)
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            await ws.recv()
        code = exc_info.value.rcvd.code if exc_info.value.rcvd else exc_info.value.code
        assert code == 1009


async def test_websocket_client_ip_cap(ws_server):
    http_url, ws_url = ws_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        created = await http_client.post("/api/rooms", json={"kind": "DEMO"})
        slug = created.json()["slug"]

    # 6th socket from the same IP -> 1013
    sockets = []
    try:
        for _ in range(5):
            s = await websockets.connect(
                f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
            )
            await s.recv()
            sockets.append(s)

        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            async with websockets.connect(
                f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
            ) as s6:
                await s6.recv()
        code = exc_info.value.rcvd.code if exc_info.value.rcvd else exc_info.value.code
        assert code == 1013
    finally:
        for s in sockets:
            await s.close()
