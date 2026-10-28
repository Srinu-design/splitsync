import asyncio
import random
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import write_tx
from app.errors import ApiError
from app.schemas import ExpenseIn
from app.seed_data import EXPENSES, MEMBERS, ROOM_NAME
from app.services.expenses import add_expense
from app.services.shared import WriteResult, bump, lock_room, room_limit, ttl, validate_members
from app.services.state import build_snapshot

# DECISION: Group members cycle through the same fixed five colors as the demo seed.
PALETTE = [color for _, color in MEMBERS]


async def create(engine, settings, body, acted_by=None):
    async def operation(connection):
        # DECISION: Serialize room creation per kind so concurrent counts cannot exceed the cap.
        await connection.execute(
            text("""
            SELECT pg_advisory_xact_lock(hashtext('splitsync:create:' || :kind))
        """),
            {"kind": body.kind},
        )
        cap = settings.MAX_ACTIVE_DEMO_ROOMS if body.kind == "DEMO" else settings.MAX_ACTIVE_GROUPS
        count = await connection.scalar(
            text("""
            SELECT count(*) FROM groups WHERE kind = :kind AND expires_at > now()
        """),
            {"kind": body.kind},
        )
        if count >= cap:
            raise ApiError(
                503,
                "ROOMS_FULL",
                "Rooms are full",
                "Try creating a room later.",
                headers={"Retry-After": "600"},
            )
        size = await connection.scalar(text("SELECT pg_database_size(current_database())"))
        if size > settings.MAX_DB_SIZE_MB * 1024 * 1024:
            raise ApiError(503, "STORAGE_FULL", "Storage is full", "New rooms are unavailable.")
        slug = secrets.token_urlsafe(8)
        gid = await connection.scalar(
            text("""
            INSERT INTO groups (slug, kind, name, expires_at)
            VALUES (:slug, :kind, :name, now() + :ttl) RETURNING id
        """),
            {
                "slug": slug,
                "kind": body.kind,
                "name": ROOM_NAME if body.kind == "DEMO" else body.name,
                "ttl": ttl(body.kind, settings),
            },
        )
        names = [name for name, _ in MEMBERS] if body.kind == "DEMO" else body.members
        if body.kind == "GROUP" and len(names) > settings.GROUP_LIMITS.members:
            room_limit("members")
        try:
            ids = await insert_members(connection, gid, names, 1)
        except IntegrityError as error:
            # DECISION: PostgreSQL owns name uniqueness when Unicode case folding differs.
            if body.kind != "GROUP" or getattr(error.orig, "sqlstate", None) != "23505":
                raise
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "Duplicate member names",
                "Group member names must be unique ignoring case.",
            ) from error
        if acted_by is not None:
            await validate_members(connection, gid, {acted_by})
        if body.kind == "DEMO":
            await seed_expenses(connection, settings, slug, ids, acted_by)
        return WriteResult({"slug": slug, "url": f"/r/{slug}", "kind": body.kind})

    return await write_tx(engine, operation)


async def insert_members(connection, gid, names, version):
    ids = {}
    for index, name in enumerate(names):
        ids[name] = await connection.scalar(
            text("""
            INSERT INTO members (group_id, version, name, color)
            VALUES (:gid, :version, :name, :color) RETURNING id
        """),
            {"gid": gid, "version": version, "name": name, "color": PALETTE[index % len(PALETTE)]},
        )
    return ids


async def seed_expenses(connection, settings, slug, ids, acted_by=None):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    for index, (title, payer, total, split_type, participants, weights, days) in enumerate(
        EXPENSES, start=1
    ):
        split_members = []
        for position, name in enumerate(participants):
            member = {"id": ids[name]}
            if split_type == "SHARES":
                member["weight"] = weights[position]
            elif split_type == "EXACT":
                member["amount"] = f"{weights[name] // 100}.{weights[name] % 100:02d}"
            split_members.append(member)
        expense = ExpenseIn(
            title=title,
            paid_by=ids[payer],
            amount=f"{total // 100}.{total % 100:02d}",
            spent_on=today - timedelta(days=days),
            split={"type": split_type, "members": split_members},
        )
        await add_expense(
            connection, settings, slug, expense, f"seed-expense-{index:016d}", acted_by
        )


async def reset(engine, settings, slug, acted_by=None):
    async def operation(connection):
        room = await lock_room(connection, slug)
        if room["kind"] != "DEMO":
            raise ApiError(
                403, "RESET_NOT_ALLOWED", "Reset not allowed", "Only demo rooms can reset."
            )
        if acted_by is not None:
            await validate_members(connection, room["id"], {acted_by})
        version = await bump(connection, room, settings)
        params = {"gid": room["id"]}
        await connection.execute(text("DELETE FROM settlements WHERE group_id=:gid"), params)
        await connection.execute(text("DELETE FROM expenses WHERE group_id=:gid"), params)
        await connection.execute(text("DELETE FROM members WHERE group_id=:gid"), params)
        ids = await insert_members(connection, room["id"], [name for name, _ in MEMBERS], version)
        # DECISION: Validate the old actor, then seed with NULL because reset removes those IDs.
        await seed_expenses(connection, settings, slug, ids)

    await write_tx(engine, operation)
    return await build_snapshot(engine, settings, slug)


async def sweep(engine, settings, redis_client=None) -> int:
    token = secrets.token_urlsafe(16)
    acquired = False
    if redis_client is not None:
        acquired = await redis_client.set_lock("splitsync:sweep", token, px=120000)
    else:
        # DECISION: When Redis is unavailable, every worker performs the idempotent sweep.
        acquired = True

    if not acquired:
        return 0

    total_deleted = 0
    try:
        while True:
            async with engine.begin() as connection:
                result = await connection.execute(
                    text("""
                    DELETE FROM groups WHERE id IN (
                        SELECT id FROM groups WHERE expires_at < now() ORDER BY expires_at LIMIT 100
                    )
                """)
                )
                deleted = result.rowcount
                total_deleted += deleted
                if deleted == 0:
                    break
    finally:
        if redis_client is not None:
            await redis_client.release_lock("splitsync:sweep", token)

    return total_deleted


async def sweep_loop(engine, settings, redis_client, interval=None, jitter=None):
    # DECISION: Use 600s ± 60s jitter by default, allowing callers to override for testing.
    base_interval = interval if interval is not None else 600.0
    jitter_range = jitter if jitter is not None else 60.0
    while True:
        try:
            sleep_time = max(0.1, base_interval + random.uniform(-jitter_range, jitter_range))
            await asyncio.sleep(sleep_time)
            await sweep(engine, settings, redis_client)
        except asyncio.CancelledError:
            break
        except Exception:
            pass
