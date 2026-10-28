import pytest
from sqlalchemy import text

from app.redis_client import make_redis
from app.services.rooms import sweep

pytestmark = pytest.mark.integration


async def test_expiry_sweep_and_redis_distributed_lock(client, app, engine):
    redis_client = make_redis("redis://localhost:6380/0")
    # Clean up lock key before testing
    await redis_client.set("splitsync:sweep", "", px=1)

    # Create two rooms: one will be expired, one will be kept live
    expired_demo = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert expired_demo.status_code == 201
    expired_slug = expired_demo.json()["slug"]

    live_demo = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert live_demo.status_code == 201
    live_slug = live_demo.json()["slug"]

    async with engine.connect() as connection:
        exp_gid = await connection.scalar(
            text("SELECT id FROM groups WHERE slug=:slug"), {"slug": expired_slug}
        )
        live_gid = await connection.scalar(
            text("SELECT id FROM groups WHERE slug=:slug"), {"slug": live_slug}
        )
        # Verify initial child rows exist for the expired room
        exp_members = await connection.scalar(
            text("SELECT count(*) FROM members WHERE group_id=:gid"), {"gid": exp_gid}
        )
        exp_expenses = await connection.scalar(
            text("SELECT count(*) FROM expenses WHERE group_id=:gid"), {"gid": exp_gid}
        )
        assert exp_members == 5
        assert exp_expenses == 8

    # Set expires_at in the past for the first room
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE groups SET expires_at=now() - interval '1 hour' WHERE id=:gid"),
            {"gid": exp_gid},
        )

    # 1. Run sweep pass: expired room and all child rows are deleted; live room is kept
    deleted = await sweep(engine, app.state.settings, redis_client=redis_client)
    assert deleted >= 1

    async with engine.connect() as connection:
        # Expired room is gone along with all child rows
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM groups WHERE id=:gid"), {"gid": exp_gid}
            )
            == 0
        )
        for table in ("members", "expenses", "expense_splits", "settlements", "reversals"):
            assert (
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE group_id=:gid"), {"gid": exp_gid}
                )
                == 0
            )

        # Live room is kept intact
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM groups WHERE id=:gid"), {"gid": live_gid}
            )
            == 1
        )

    # 2. While another token holds splitsync:sweep, the pass does nothing
    await redis_client.set("splitsync:sweep", "other_worker_token", px=120000)

    # Expire the second room
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE groups SET expires_at=now() - interval '1 hour' WHERE id=:gid"),
            {"gid": live_gid},
        )

    # DECISION: A locked sweep pass exits with 0 deleted rows and leaves expired rooms untouched.
    noop_deleted = await sweep(engine, app.state.settings, redis_client=redis_client)
    assert noop_deleted == 0

    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM groups WHERE id=:gid"), {"gid": live_gid}
            )
            == 1
        )

    # 3. The release script never deletes someone else's lock
    released_wrong = await redis_client.release_lock("splitsync:sweep", "wrong_token")
    assert released_wrong is False
    assert await redis_client.get("splitsync:sweep") == "other_worker_token"

    # Releasing with matching token clears the lock
    released_correct = await redis_client.release_lock("splitsync:sweep", "other_worker_token")
    assert released_correct is True
    assert await redis_client.get("splitsync:sweep") is None

    # Clean up second room with final sweep
    final_deleted = await sweep(engine, app.state.settings, redis_client=redis_client)
    assert final_deleted >= 1
    await redis_client.aclose()
