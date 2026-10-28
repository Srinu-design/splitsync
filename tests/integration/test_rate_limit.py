import asyncio
import json
import logging
import socket

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
import uvicorn
import websockets
import websockets.exceptions

from app.api.rate_limit import DEFAULT_RATE_LIMITS, RATE_LIMITS
from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(autouse=True)
async def reset_rate_limits():
    # DECISION: Restore DEFAULT_RATE_LIMITS and delete rl:* keys before/after each test.
    RATE_LIMITS.clear()
    RATE_LIMITS.update(DEFAULT_RATE_LIMITS)
    client = aioredis.from_url("redis://localhost:6380/0")
    try:
        keys = await client.keys("rl:*")
        if keys:
            await client.delete(*keys)
    except Exception:
        pass
    finally:
        await client.aclose()
    yield
    RATE_LIMITS.clear()
    RATE_LIMITS.update(DEFAULT_RATE_LIMITS)
    client = aioredis.from_url("redis://localhost:6380/0")
    try:
        keys = await client.keys("rl:*")
        if keys:
            await client.delete(*keys)
    except Exception:
        pass
    finally:
        await client.aclose()


@pytest.fixture
def rate_limit_settings(migrated_database):
    url, _ = migrated_database
    return Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://localhost:6380/0",
        RATE_LIMIT_ENABLED=True,
        ALLOWED_ORIGINS=["http://localhost:8000"],
        DB_POOL_SIZE=12,
        DB_MAX_OVERFLOW=0,
    )


@pytest.fixture
async def rl_app(rate_limit_settings):
    application = create_app(rate_limit_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def rl_client(rl_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rl_app), base_url="http://test"
    ) as client:
        yield client


async def test_rate_limit_writes(rl_client):
    # Patch write limit to 3 per minute
    RATE_LIMITS["write"] = (3, 60)

    room_res = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert room_res.status_code == 201
    slug = room_res.json()["slug"]

    # 3 writes should succeed
    for i in range(1, 4):
        res = await rl_client.post(
            f"/api/rooms/{slug}/members",
            json={"name": f"Member_{i}"},
        )
        assert res.status_code == 201, res.text

    # 4th write should be rejected with 429 RATE_LIMITED and Retry-After
    res4 = await rl_client.post(
        f"/api/rooms/{slug}/members",
        json={"name": "Member_4"},
    )
    assert res4.status_code == 429
    assert res4.headers["content-type"] == "application/problem+json"
    retry_after = res4.headers.get("retry-after")
    assert retry_after is not None
    assert int(retry_after) > 0
    problem = res4.json()
    assert problem["code"] == "RATE_LIMITED"
    assert problem["status"] == 429
    assert problem["type"] == "/errors/rate-limited"


async def test_rate_limit_room_creation(rl_client):
    # Patch room creation to 2 per hour
    RATE_LIMITS["create_room"] = (2, 3600)

    res1 = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert res1.status_code == 201

    res2 = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert res2.status_code == 201

    # 3rd create room should return 429
    res3 = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert res3.status_code == 429
    assert int(res3.headers["retry-after"]) > 0
    assert res3.json()["code"] == "RATE_LIMITED"


async def test_rate_limit_naive_mode(rl_client):
    # Patch naive to 2 per minute, write to 60 per minute
    RATE_LIMITS["naive"] = (2, 60)
    RATE_LIMITS["write"] = (60, 60)

    room_res = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert room_res.status_code == 201
    slug = room_res.json()["slug"]

    state_res = await rl_client.get(f"/api/rooms/{slug}/state")
    members = state_res.json()["members"]
    m1, m2 = members[0]["id"], members[1]["id"]

    for i in range(1, 3):
        res = await rl_client.post(
            f"/api/rooms/{slug}/settlements",
            headers={"Idempotency-Key": f"naive-idem-key-{i:04d}"},
            json={
                "from_member": m1,
                "to_member": m2,
                "amount": "10.00",
                "mode": "NAIVE",
                "expected_graph_version": 9,
            },
        )
        assert res.status_code == 201, res.text

    # 3rd naive request exceeds naive limit
    res3 = await rl_client.post(
        f"/api/rooms/{slug}/settlements",
        headers={"Idempotency-Key": "naive-idem-key-0003"},
        json={
            "from_member": m1,
            "to_member": m2,
            "amount": "10.00",
            "mode": "NAIVE",
            "expected_graph_version": 9,
        },
    )
    assert res3.status_code == 429
    assert res3.json()["code"] == "RATE_LIMITED"
    assert int(res3.headers["retry-after"]) > 0


async def test_rate_limit_search_and_history(rl_client):
    # Patch search & history read limit to 2 per minute
    RATE_LIMITS["read"] = (2, 60)

    room_res = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert room_res.status_code == 201
    slug = room_res.json()["slug"]

    res1 = await rl_client.get(f"/api/rooms/{slug}/expenses?q=vil")
    assert res1.status_code == 200

    res2 = await rl_client.get(f"/api/rooms/{slug}/activity")
    assert res2.status_code == 200

    # 3rd read should be rejected with 429
    res3 = await rl_client.get(f"/api/rooms/{slug}/expenses")
    assert res3.status_code == 429
    assert res3.json()["code"] == "RATE_LIMITED"
    assert int(res3.headers["retry-after"]) > 0


async def test_rate_limit_redis_down_fail_open(migrated_database):
    url, _ = migrated_database
    # Point Redis at an unused port (6399)
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://127.0.0.1:6399/0",
        RATE_LIMIT_ENABLED=True,
        ALLOWED_ORIGINS=["http://localhost:8000"],
        DB_POOL_SIZE=12,
        DB_MAX_OVERFLOW=0,
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            RATE_LIMITS["write"] = (2, 60)
            room_res = await client.post("/api/rooms", json={"kind": "DEMO"})
            assert room_res.status_code == 201
            slug = room_res.json()["slug"]

            # With Redis down, requests beyond patched limit (2) all succeed
            for i in range(1, 5):
                res = await client.post(
                    f"/api/rooms/{slug}/members",
                    json={"name": f"FailOpen_{i}"},
                )
                assert res.status_code == 201, res.text


async def test_rate_limit_websocket_connect(migrated_database):
    url, _ = migrated_database
    port = find_free_port()
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://localhost:6380/0",
        ALLOWED_ORIGINS=["http://localhost:8000"],
        RATE_LIMIT_ENABLED=True,
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
        http_url = f"http://127.0.0.1:{port}"
        ws_url = f"ws://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=http_url) as client:
            created = await client.post("/api/rooms", json={"kind": "DEMO"})
            assert created.status_code == 201
            slug = created.json()["slug"]

        # Patch ws_connect limit to 2 per minute
        RATE_LIMITS["ws_connect"] = (2, 60)

        # 1st connection succeeds
        async with websockets.connect(
            f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
        ) as ws1:
            msg1 = json.loads(await asyncio.wait_for(ws1.recv(), timeout=2.0))
            assert msg1["type"] == "snapshot"

        # 2nd connection succeeds
        async with websockets.connect(
            f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
        ) as ws2:
            msg2 = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2.0))
            assert msg2["type"] == "snapshot"

        # 3rd connection is closed with 1013
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
            async with websockets.connect(
                f"{ws_url}/ws/rooms/{slug}", origin="http://localhost:8000"
            ) as ws3:
                await ws3.recv()
        code = exc_info.value.rcvd.code if exc_info.value.rcvd else exc_info.value.code
        assert code == 1013
    finally:
        server.should_exit = True
        await task


async def test_payload_too_large_16kb(rl_client):
    # 16 KB + 1 byte = 16,385 bytes
    oversized_name = "A" * (16 * 1024 + 1)
    large_payload = {"kind": "DEMO", "name": oversized_name}
    res = await rl_client.post(
        "/api/rooms",
        content=json.dumps(large_payload),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413
    assert res.headers["content-type"] == "application/problem+json"
    problem = res.json()
    assert problem["code"] == "PAYLOAD_TOO_LARGE"
    assert problem["status"] == 413
    assert problem["type"] == "/errors/payload-too-large"

    # Normal body still works
    res_normal = await rl_client.post("/api/rooms", json={"kind": "DEMO"})
    assert res_normal.status_code == 201


async def test_x_request_id_and_json_logging(rl_client, caplog):
    caplog.set_level(logging.INFO, logger="splitsync.access")

    # 1. Without incoming X-Request-ID: generated ID returned
    res1 = await rl_client.get("/api/rooms/unknown-slug/state")
    req_id1 = res1.headers.get("x-request-id")
    assert req_id1 is not None
    assert 8 <= len(req_id1) <= 64

    # 2. Valid incoming X-Request-ID: kept
    custom_id = "valid-client-id-12345"
    res2 = await rl_client.get("/api/rooms/unknown-slug/state", headers={"X-Request-ID": custom_id})
    assert res2.headers.get("x-request-id") == custom_id

    # 3. Invalid incoming X-Request-ID (too short): replaced
    res3 = await rl_client.get("/api/rooms/unknown-slug/state", headers={"X-Request-ID": "short"})
    req_id3 = res3.headers.get("x-request-id")
    assert req_id3 != "short"
    assert 8 <= len(req_id3) <= 64

    # Clear captured logs before testing the 4 outcome scenarios
    caplog.clear()

    # (a) 201 -> outcome CREATED
    room_res = await rl_client.post(
        "/api/rooms",
        json={"kind": "DEMO"},
        headers={"X-Request-ID": "test-created-001"},
    )
    assert room_res.status_code == 201
    slug = room_res.json()["slug"]

    # (b) Replay -> outcome REPLAYED
    state_res = await rl_client.get(f"/api/rooms/{slug}/state")
    members = state_res.json()["members"]
    aarav_id = next(m["id"] for m in members if m["name"] == "Aarav")
    priya_id = next(m["id"] for m in members if m["name"] == "Priya")

    exp_payload = {
        "title": "Secret Dinner Expense",
        "paid_by": aarav_id,
        "amount": "100.00",
        "split": {"type": "EQUAL", "members": [{"id": m["id"]} for m in members]},
    }
    exp_res1 = await rl_client.post(
        f"/api/rooms/{slug}/expenses",
        headers={"Idempotency-Key": "replay-test-key-01", "X-Request-ID": "test-exp-create-01"},
        json=exp_payload,
    )
    assert exp_res1.status_code == 201

    exp_res2 = await rl_client.post(
        f"/api/rooms/{slug}/expenses",
        headers={"Idempotency-Key": "replay-test-key-01", "X-Request-ID": "test-exp-replay-01"},
        json=exp_payload,
    )
    assert exp_res2.status_code == 200
    assert exp_res2.headers.get("Idempotent-Replayed") == "true"

    # (c) 409 STALE -> outcome STALE (stale expected_graph_version on settlement)
    stale_res = await rl_client.post(
        f"/api/rooms/{slug}/settlements",
        headers={"Idempotency-Key": "stale-settle-key-01", "X-Request-ID": "test-stale-001"},
        json={
            "from_member": aarav_id,
            "to_member": priya_id,
            "amount": "10.00",
            "expected_graph_version": 1,  # Stale version! (current is 10)
        },
    )
    assert stale_res.status_code == 409
    assert stale_res.json()["code"] == "STALE_GRAPH_VERSION"

    # (d) 429 RATE_LIMITED -> outcome RATE_LIMITED
    RATE_LIMITS["write"] = (1, 60)
    # The settlement attempt above was 1 write, so this next write will hit 429
    rl_res = await rl_client.post(
        f"/api/rooms/{slug}/members",
        headers={"X-Request-ID": "test-rate-limit-01"},
        json={"name": "Karthik2"},
    )
    assert rl_res.status_code == 429

    # (e) Verify /healthz does not produce a log line
    await rl_client.get("/healthz")

    # Assert captured JSON log lines
    log_lines = [json.loads(record.getMessage()) for record in caplog.records]
    outcomes = {log["request_id"]: log for log in log_lines}

    # Verify 201 outcome CREATED
    created_log = outcomes["test-created-001"]
    assert created_log["method"] == "POST"
    assert created_log["path"] == "/api/rooms"
    assert created_log["status"] == 201
    assert created_log["room_slug"] is None
    assert created_log["outcome"] == "CREATED"
    assert isinstance(created_log["latency_ms"], (int, float))

    # Verify replay outcome REPLAYED
    replay_log = outcomes["test-exp-replay-01"]
    assert replay_log["status"] == 200
    assert replay_log["outcome"] == "REPLAYED"
    assert replay_log["room_slug"] == slug

    # Verify 409 STALE outcome STALE
    stale_log = outcomes["test-stale-001"]
    assert stale_log["status"] == 409
    assert stale_log["outcome"] == "STALE"
    assert stale_log["room_slug"] == slug

    # Verify 429 outcome RATE_LIMITED
    rl_log = outcomes["test-rate-limit-01"]
    assert rl_log["status"] == 429
    assert rl_log["outcome"] == "RATE_LIMITED"
    assert rl_log["room_slug"] == slug

    # Verify /healthz was never logged
    assert not any(log["path"] == "/healthz" for log in log_lines)

    # CRITICAL: Assert no request body text appears in any log line!
    for record in caplog.records:
        assert "Secret Dinner Expense" not in record.getMessage()
