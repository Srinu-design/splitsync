import asyncio
import json

from sqlalchemy import text

from app.db import write_tx
from app.domain.ledger import settlement_deltas
from app.errors import ApiError
from app.services.shared import (
    WriteResult,
    apply_deltas,
    bump,
    limits,
    lock_room,
    lookup_idempotency,
    payload_hash,
    room_limit,
    validate_members,
)


async def insert_settlement(connection, room, body, key, acted_by, request_hash, version, before):
    sid = await connection.scalar(text("SELECT nextval('settlements_id_seq')"))
    receipt = {
        "settlement_id": sid,
        "from_member": body.from_member,
        "to_member": body.to_member,
        "amount_paise": body.amount_paise,
        "graph_version_before": before,
        "graph_version_after": version,
    }
    await connection.execute(
        text("""
        INSERT INTO settlements
            (id, group_id, version, from_member, to_member, acted_by, amount_paise, mode,
             idempotency_key, payload_hash, graph_version_before, response_json)
        VALUES (:sid, :gid, :version, :sender, :receiver, :actor, :amount, :mode,
                :key, :hash, :before, CAST(:response AS jsonb))
    """),
        {
            "sid": sid,
            "gid": room["id"],
            "version": version,
            "sender": body.from_member,
            "receiver": body.to_member,
            "actor": acted_by,
            "amount": body.amount_paise,
            "mode": body.mode,
            "key": key,
            "hash": request_hash,
            "before": before,
            "response": json.dumps(receipt),
        },
    )
    return WriteResult(receipt)


async def member_balances(connection, room, body, acted_by, *, locked):
    ids = {body.from_member, body.to_member}
    if acted_by is not None:
        ids.add(acted_by)
    query = """
        SELECT id, net_balance_paise FROM members
        WHERE group_id = :gid AND id = ANY(:ids) ORDER BY id
    """ + (" FOR UPDATE" if locked else "")
    balances = dict(
        (await connection.execute(text(query), {"gid": room["id"], "ids": sorted(ids)})).all()
    )
    if set(balances) != ids:
        raise ApiError(
            422,
            "MEMBER_NOT_IN_GROUP",
            "Member not in room",
            "Every referenced member must belong to this room.",
        )
    return balances


async def create(engine, settings, slug, body, key, acted_by=None):
    request_hash = payload_hash("settle", slug, body.from_member, body.to_member, body.amount_paise)

    async def protected(connection):
        room = await lock_room(connection, slug)
        replay = await lookup_idempotency(connection, room["id"], key, request_hash)
        if replay is not None:
            if acted_by is not None:
                await validate_members(connection, room["id"], {acted_by})
            return replay
        before = room["graph_version"]
        if before != body.expected_graph_version:
            raise ApiError(
                409,
                "STALE_GRAPH_VERSION",
                "Balances changed",
                "Refresh the plan before recording this payment.",
                current_graph_version=before,
            )
        balances = await member_balances(connection, room, body, acted_by, locked=True)
        sender, receiver = balances[body.from_member], balances[body.to_member]
        maximum = min(-sender, receiver) if sender < 0 and receiver > 0 else 0
        if body.amount_paise > maximum:
            raise ApiError(
                422,
                "OVER_SETTLEMENT",
                "Payment exceeds the debt",
                "The payment must fit both members' balances.",
                max_paise=maximum,
            )
        count = await connection.scalar(
            text("SELECT count(*) FROM settlements WHERE group_id=:gid"), {"gid": room["id"]}
        )
        if count >= limits(room, settings).settlements:
            room_limit("settlements")
        await apply_deltas(
            connection,
            room["id"],
            settlement_deltas(body.from_member, body.to_member, body.amount_paise),
        )
        version = await bump(connection, room, settings)
        return await insert_settlement(
            connection, room, body, key, acted_by, request_hash, version, before
        )

    if body.mode == "PROTECTED":
        return await write_tx(engine, protected)

    async def naive(connection):
        room = (
            (
                await connection.execute(
                    text("""
            SELECT id, kind, graph_version FROM groups WHERE slug=:slug AND expires_at > now()
        """),
                    {"slug": slug},
                )
            )
            .mappings()
            .one_or_none()
        )
        if room is None:
            raise ApiError(
                404, "ROOM_NOT_FOUND", "Room not found", "The room is missing or expired."
            )
        if not settings.ENABLE_NAIVE_MODE or room["kind"] != "DEMO":
            raise ApiError(
                403,
                "NAIVE_MODE_DISABLED",
                "Naive mode disabled",
                "This demonstration is available only in enabled demo rooms.",
            )
        balances = await member_balances(connection, room, body, acted_by, locked=False)
        await asyncio.sleep(settings.NAIVE_RACE_DELAY_MS / 1000)
        # Intentional demo bug: absolute writes overwrite other requests' cached balance changes.
        for member, delta in sorted(
            settlement_deltas(body.from_member, body.to_member, body.amount_paise).items()
        ):
            await connection.execute(
                text("""
                UPDATE members SET net_balance_paise = :balance WHERE group_id=:gid AND id=:member
            """),
                {"gid": room["id"], "member": member, "balance": balances[member] + delta},
            )
        # DECISION: A naive receipt reports its ledger row's read_version + 1, as stored on insert.
        receipt = await insert_settlement(
            connection,
            room,
            body,
            key,
            acted_by,
            request_hash,
            room["graph_version"] + 1,
            room["graph_version"],
        )
        return receipt, room["id"]

    result, gid = await write_tx(engine, naive)
    # Separate commit prevents a member-lock -> room-lock cycle with protected writers.
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            UPDATE groups SET graph_version = graph_version + 1 WHERE id=:gid
        """),
            {"gid": gid},
        )
    return result
