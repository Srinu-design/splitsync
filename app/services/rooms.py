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
from app.services.shared import WriteResult, room_limit, ttl, validate_members

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
        ids = {}
        try:
            for index, name in enumerate(names):
                ids[name] = await connection.scalar(
                    text("""
                    INSERT INTO members (group_id, version, name, color)
                    VALUES (:gid, 1, :name, :color) RETURNING id
                """),
                    {"gid": gid, "name": name, "color": PALETTE[index % len(PALETTE)]},
                )
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
        return WriteResult({"slug": slug, "url": f"/r/{slug}", "kind": body.kind})

    return await write_tx(engine, operation)
