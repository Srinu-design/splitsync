import asyncio
import json
import secrets

import httpx
import pytest
import websockets
import websockets.exceptions


async def test_multiworker_redis_fanout():
    # DECISION: Skip cleanly if the compose stack on http://localhost:8300 is not running.
    base_url = "http://localhost:8300"
    ws_base_url = "ws://localhost:8300"

    try:
        async with httpx.AsyncClient(base_url=base_url) as test_client:
            health = await test_client.get("/healthz", timeout=2.0)
            if health.status_code != 200:
                pytest.skip("Compose app is not healthy on http://localhost:8300")
    except Exception:
        pytest.skip("Docker Compose app at http://localhost:8300 is not reachable")

    async with httpx.AsyncClient(base_url=base_url) as client:
        room_res = await client.post("/api/rooms", json={"kind": "DEMO"})
        assert room_res.status_code == 201
        slug = room_res.json()["slug"]

        open_sockets = []
        worker_pids = set()
        attempts = 0
        max_attempts = 40

        # DECISION: Open sockets until at least two distinct worker PIDs are seen, max 40 tries.
        while len(worker_pids) < 2 and attempts < max_attempts:
            attempts += 1
            try:
                ws = await websockets.connect(
                    f"{ws_base_url}/ws/rooms/{slug}",
                    origin="http://localhost:8300",
                )
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(msg_raw)
                assert msg["type"] == "snapshot"
                pid = msg.get("worker_pid")
                assert pid is not None, "worker_pid must be present when EXPOSE_WORKER_PID=true"
                worker_pids.add(pid)
                open_sockets.append(ws)
            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else exc.code
                if code == 1013:
                    continue
                raise

        assert len(worker_pids) >= 2, (
            f"Failed to cover 2 worker PIDs after {attempts} attempts. Seen: {worker_pids}"
        )

        try:
            # POST one expense to trigger Redis Pub/Sub broadcast across all workers
            state_res = await client.get(f"/api/rooms/{slug}/state")
            members = state_res.json()["members"]
            aarav_id = next(m["id"] for m in members if m["name"] == "Aarav")

            expense_res = await client.post(
                f"/api/rooms/{slug}/expenses",
                headers={"Idempotency-Key": f"e2e-fanout-{secrets.token_hex(8)}"},
                json={
                    "title": "E2E Fanout Verification",
                    "paid_by": aarav_id,
                    "amount": "100",
                    "split": {"type": "EQUAL", "members": [{"id": m["id"]} for m in members]},
                },
            )
            assert expense_res.status_code == 201

            # Assert EVERY open socket on EVERY worker receives the new snapshot within 2 s
            for ws in open_sockets:
                update_raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                update_msg = json.loads(update_raw)
                assert update_msg["type"] == "snapshot"
                assert update_msg["version"] == 10
        finally:
            for ws in open_sockets:
                try:
                    await ws.close()
                except Exception:
                    pass
