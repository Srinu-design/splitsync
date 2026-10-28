import asyncio
import json
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
import websockets
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from app.services.rooms import sweep

pytestmark = pytest.mark.integration


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def degraded_server(migrated_database):
    url, _ = migrated_database
    port = find_free_port()
    # DECISION: Point degraded test server at unused Redis port 6399 to verify fail-open behavior.
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://127.0.0.1:6399/0",
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
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}", application
    finally:
        server.should_exit = True
        await task


async def test_readyz_reports_degraded_when_redis_is_down(degraded_server):
    http_url, _, _ = degraded_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        res = await http_client.get("/readyz")
        assert res.status_code == 200
        assert res.json() == {"db": "ok", "redis": "degraded"}


async def test_writes_succeed_and_local_broadcast_works_when_redis_is_down(degraded_server, engine):
    http_url, ws_url, app_instance = degraded_server
    async with httpx.AsyncClient(base_url=http_url) as http_client:
        created = await http_client.post("/api/rooms", json={"kind": "DEMO"})
        assert created.status_code == 201
        slug = created.json()["slug"]

        # Connect WebSocket to room
        async with websockets.connect(
            f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
        ) as ws:
            first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert first_msg["type"] == "snapshot"
            assert first_msg["version"] == 9

            # 1. Add expense succeeds
            state_res = await http_client.get(f"/api/rooms/{slug}/state")
            members = state_res.json()["members"]
            aarav = next(m["id"] for m in members if m["name"] == "Aarav")
            karthik = next(m["id"] for m in members if m["name"] == "Karthik")

            add_res = await http_client.post(
                f"/api/rooms/{slug}/expenses",
                headers={"Idempotency-Key": "down-test-exp-0001"},
                json={
                    "title": "Taxi",
                    "paid_by": aarav,
                    "amount": "100",
                    "split": {"type": "EQUAL", "members": [{"id": m["id"]} for m in members]},
                },
            )
            assert add_res.status_code == 201
            expense_id = add_res.json()["expense_id"]

            # Local broadcast fallback delivers snapshot to the connected socket
            ws_update = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert ws_update["type"] == "snapshot"
            assert ws_update["version"] == 10

            # 2. Protected settlement succeeds
            settle_res = await http_client.post(
                f"/api/rooms/{slug}/settlements",
                headers={"Idempotency-Key": "down-test-settle-001"},
                json={
                    "from_member": karthik,
                    "to_member": aarav,
                    "amount": "100",
                    "expected_graph_version": 10,
                    "mode": "PROTECTED",
                },
            )
            assert settle_res.status_code == 201

            # 3. Edit expense succeeds
            edit_res = await http_client.put(
                f"/api/rooms/{slug}/expenses/{expense_id}",
                headers={"Idempotency-Key": "down-test-edit-0001"},
                json={
                    "title": "Taxi updated",
                    "paid_by": aarav,
                    "amount": "200",
                    "split": {"type": "EQUAL", "members": [{"id": m["id"]} for m in members]},
                },
            )
            assert edit_res.status_code == 200

    # 4. One sweep pass still deletes an expired room without Redis
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            INSERT INTO groups (slug, kind, name, graph_version, expires_at)
            VALUES ('exp_down_slug', 'DEMO', 'Expired Demo', 1, now() - interval '1 hour')
        """)
        )

    deleted = await sweep(
        app_instance.state.engine,
        app_instance.state.settings,
        redis_client=app_instance.state.redis,
    )
    assert deleted >= 1

    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM groups WHERE slug='exp_down_slug'")
        )
        assert count == 0
