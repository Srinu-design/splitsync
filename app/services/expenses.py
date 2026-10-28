import json

from sqlalchemy import text

from app.db import write_tx
from app.domain.allocation import allocate
from app.domain.ledger import expense_deltas
from app.domain.money import parse_rupees
from app.errors import ApiError
from app.services.shared import (
    WriteResult,
    apply_deltas,
    bump,
    canonical_body,
    limits,
    lock_room,
    lookup_idempotency,
    payload_hash,
    room_limit,
    validate_members,
)


async def add_expense(connection, settings, slug, body, key, acted_by=None):
    request_hash = payload_hash("expense", slug, canonical_body(body))
    room = await lock_room(connection, slug)
    replay = await lookup_idempotency(connection, room["id"], key, request_hash)
    if replay is not None:
        if acted_by is not None:
            await validate_members(connection, room["id"], {acted_by})
        return replay
    cap = limits(room, settings)
    count = await connection.scalar(
        text("SELECT count(*) FROM expenses WHERE group_id = :gid"), {"gid": room["id"]}
    )
    if count >= cap.expense_rows:
        room_limit("expenses")
    participants = [member.id for member in body.split.members]
    if len(participants) > cap.members:
        room_limit("members")
    ids = {body.paid_by, *participants}
    if acted_by is not None:
        ids.add(acted_by)
    await validate_members(connection, room["id"], ids)
    version = await bump(connection, room, settings)
    expense_id = await connection.scalar(text("SELECT nextval('expenses_id_seq')"))
    total = body.total_paise
    if body.split.type == "EXACT":
        shares = {member.id: parse_rupees(member.amount) for member in body.split.members}
        if sum(shares.values()) != total:
            raise ApiError(
                422,
                "EXACT_SUM_MISMATCH",
                "Exact shares do not match",
                "The shares must add up to the expense total.",
                expected_paise=total,
                got_paise=sum(shares.values()),
            )
    else:
        weights = (
            [member.weight for member in body.split.members]
            if body.split.type == "SHARES"
            else None
        )
        shares = allocate(total, participants, weights, rotation=expense_id)
    response = {
        "expense_id": expense_id,
        "lineage_id": expense_id,
        "graph_version": version,
        "shares": [
            {"member_id": member, "share_paise": amount}
            for member, amount in sorted(shares.items())
        ],
    }
    await connection.execute(
        text("""
        INSERT INTO expenses
            (id, group_id, version, lineage_id, paid_by, acted_by, title, spent_on,
             total_paise, split_type, idempotency_key, payload_hash, response_json)
        VALUES (:eid, :gid, :version, :eid, :payer, :actor, :title, :spent_on,
                :total, :split_type, :key, :hash, CAST(:response AS jsonb))
    """),
        {
            "eid": expense_id,
            "gid": room["id"],
            "version": version,
            "payer": body.paid_by,
            "actor": acted_by,
            "title": body.title,
            "spent_on": body.spent_on,
            "total": total,
            "split_type": body.split.type,
            "key": key,
            "hash": request_hash,
            "response": json.dumps(response),
        },
    )
    await connection.execute(
        text("""
        INSERT INTO expense_splits (group_id, expense_id, member_id, weight, share_paise)
        VALUES (:gid, :eid, :member, :weight, :share)
    """),
        [
            {
                "gid": room["id"],
                "eid": expense_id,
                "member": member.id,
                "weight": getattr(member, "weight", None),
                "share": shares[member.id],
            }
            for member in body.split.members
        ],
    )
    await apply_deltas(connection, room["id"], expense_deltas(body.paid_by, total, shares))
    return WriteResult(response)


async def create(engine, settings, slug, body, key, acted_by=None):
    async def operation(connection):
        return await add_expense(connection, settings, slug, body, key, acted_by)

    return await write_tx(engine, operation)
