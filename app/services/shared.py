import hashlib
import json
import re
from datetime import timedelta
from typing import NamedTuple

from sqlalchemy import text

from app.errors import ApiError


class WriteResult(NamedTuple):
    body: dict
    status: int = 201
    headers: dict | None = None


def payload_hash(*parts) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def canonical_body(body) -> str:
    return json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def limits(room, settings):
    return settings.DEMO_LIMITS if room["kind"] == "DEMO" else settings.GROUP_LIMITS


def ttl(kind, settings):
    return (
        timedelta(hours=settings.DEMO_TTL_HOURS)
        if kind == "DEMO"
        else timedelta(days=settings.GROUP_TTL_DAYS)
    )


async def lock_room(connection, slug):
    check_slug(slug)
    room = (
        (
            await connection.execute(
                text("""
        SELECT id, kind, graph_version FROM groups
        WHERE slug = :slug AND expires_at > now() FOR UPDATE
    """),
                {"slug": slug},
            )
        )
        .mappings()
        .one_or_none()
    )
    if room is None:
        raise ApiError(404, "ROOM_NOT_FOUND", "Room not found", "The room is missing or expired.")
    return dict(room)


def check_slug(slug):
    if re.fullmatch(r"[A-Za-z0-9_-]{8,32}", slug) is None:
        raise ApiError(404, "ROOM_NOT_FOUND", "Room not found", "The room is missing or expired.")


async def read_room(connection, slug):
    check_slug(slug)
    room = (
        (
            await connection.execute(
                text("""
        SELECT id, slug, kind, name, graph_version, expires_at FROM groups
        WHERE slug = :slug AND expires_at > now()
    """),
                {"slug": slug},
            )
        )
        .mappings()
        .one_or_none()
    )
    if room is None:
        raise ApiError(404, "ROOM_NOT_FOUND", "Room not found", "The room is missing or expired.")
    return dict(room)


async def lookup_idempotency(connection, gid, key, request_hash):
    row = (
        (
            await connection.execute(
                text("""
SELECT payload_hash, response_json FROM expenses
 WHERE group_id = :gid AND idempotency_key = :key
UNION ALL
SELECT payload_hash, response_json FROM settlements
 WHERE group_id = :gid AND idempotency_key = :key AND mode = 'PROTECTED'
UNION ALL
SELECT payload_hash, response_json FROM reversals
 WHERE group_id = :gid AND idempotency_key = :key;
    """),
                {"gid": gid, "key": key},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["payload_hash"] != request_hash:
        raise ApiError(
            422,
            "IDEMPOTENCY_KEY_REUSED",
            "Key already used",
            "Use a new idempotency key for a different request.",
        )
    return WriteResult(row["response_json"], 200, {"Idempotent-Replayed": "true"})


async def bump(connection, room, settings):
    version = await connection.scalar(
        text("""
        UPDATE groups SET graph_version = graph_version + 1, last_active_at = now(),
                          expires_at = now() + :ttl
        WHERE id = :gid RETURNING graph_version
    """),
        {"gid": room["id"], "ttl": ttl(room["kind"], settings)},
    )
    room["graph_version"] = version
    return version


async def validate_members(connection, gid, ids):
    found = set(
        await connection.scalars(
            text("""
        SELECT id FROM members WHERE group_id = :gid AND id = ANY(:ids)
    """),
            {"gid": gid, "ids": sorted(ids)},
        )
    )
    if found != set(ids):
        raise ApiError(
            422,
            "MEMBER_NOT_IN_GROUP",
            "Member not in room",
            "Every referenced member must belong to this room.",
        )


async def apply_deltas(connection, gid, deltas):
    for member, delta in sorted(deltas.items()):
        if delta:
            await connection.execute(
                text("""
                UPDATE members SET net_balance_paise = net_balance_paise + :delta
                WHERE group_id = :gid AND id = :member
            """),
                {"gid": gid, "member": member, "delta": delta},
            )


def room_limit(name):
    raise ApiError(
        422,
        "ROOM_LIMIT",
        "Room limit reached",
        "This room has reached its configured limit.",
        limit=name,
    )
