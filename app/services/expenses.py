import json

from sqlalchemy import text

from app.db import write_tx
from app.domain.allocation import allocate
from app.domain.ledger import expense_deltas, merge, negate
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
    await validate_expense(connection, room, settings, body, acted_by)
    version = await bump(connection, room, settings)
    expense_id = await connection.scalar(text("SELECT nextval('expenses_id_seq')"))
    shares = calculate_shares(body, expense_id)
    response = {
        "expense_id": expense_id,
        "lineage_id": expense_id,
        "graph_version": version,
        "shares": [
            {"member_id": member, "share_paise": amount}
            for member, amount in sorted(shares.items())
        ],
    }
    await insert_expense(connection, room, body, key, acted_by, request_hash, response, shares)
    await apply_deltas(
        connection, room["id"], expense_deltas(body.paid_by, body.total_paise, shares)
    )
    return WriteResult(response)


async def create(engine, settings, slug, body, key, acted_by=None):
    async def operation(connection):
        return await add_expense(connection, settings, slug, body, key, acted_by)

    return await write_tx(engine, operation)


async def check_capacity(connection, room, settings):
    cap = limits(room, settings)
    count = await connection.scalar(
        text("SELECT count(*) FROM expenses WHERE group_id = :gid"), {"gid": room["id"]}
    )
    if count >= cap.expense_rows:
        room_limit("expenses")


async def validate_expense(connection, room, settings, body, acted_by):
    await check_capacity(connection, room, settings)
    cap = limits(room, settings)
    participants = [member.id for member in body.split.members]
    if len(participants) > cap.members:
        room_limit("members")
    ids = {body.paid_by, *participants}
    if acted_by is not None:
        ids.add(acted_by)
    await validate_members(connection, room["id"], ids)


def calculate_shares(body, expense_id):
    participants = [member.id for member in body.split.members]
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
    return shares


async def insert_expense(
    connection, room, body, key, acted_by, request_hash, response, shares, *, revision_of=None
):
    expense_id = response["expense_id"]
    version = response["graph_version"]
    total = body.total_paise
    await connection.execute(
        text("""
        INSERT INTO expenses
            (id, group_id, version, lineage_id, revision_of, paid_by, acted_by, title, spent_on,
             total_paise, split_type, idempotency_key, payload_hash, response_json)
        VALUES (:eid, :gid, :version, :lineage, :revision, :payer, :actor, :title, :spent_on,
                :total, :split_type, :key, :hash, CAST(:response AS jsonb))
    """),
        {
            "eid": expense_id,
            "gid": room["id"],
            "lineage": response["lineage_id"],
            "revision": revision_of,
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


async def load_expense(connection, room, expense_id):
    target = (
        (
            await connection.execute(
                text("""
        SELECT e.*, r.reason AS reversal_reason FROM expenses e
        LEFT JOIN reversals r ON r.expense_id = e.id
        WHERE e.group_id = :gid AND e.id = :eid
    """),
                {"gid": room["id"], "eid": expense_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if target is None:
        raise ApiError(
            404,
            "EXPENSE_NOT_FOUND",
            "Expense not found",
            "This expense does not exist in this room.",
        )
    splits = (
        (
            await connection.execute(
                text("""
        SELECT member_id, weight, share_paise FROM expense_splits
        WHERE group_id = :gid AND expense_id = :eid ORDER BY member_id
    """),
                {"gid": room["id"], "eid": expense_id},
            )
        )
        .mappings()
        .all()
    )
    return target, splits


async def expense_changed(connection, room, target):
    current = (
        (
            await connection.execute(
                text("""
SELECT e.id, r.reason FROM expenses e LEFT JOIN reversals r ON r.expense_id = e.id
WHERE e.group_id = :gid AND e.lineage_id = :lineage
  AND NOT EXISTS (SELECT 1 FROM expenses s WHERE s.revision_of = e.id);
    """),
                {"gid": room["id"], "lineage": target["lineage_id"]},
            )
        )
        .mappings()
        .one()
    )
    deleted = current["reason"] == "DELETE"
    if deleted:
        detail = f"Version {target['id']} was deleted."
    elif current["id"] != target["id"]:
        detail = f"Version {target['id']} was replaced by version {current['id']}."
    else:
        detail = f"Version {target['id']} is still active."
    raise ApiError(
        409,
        "EXPENSE_CHANGED",
        "Expense changed",
        detail,
        current_expense_id=None if deleted else current["id"],
        deleted=deleted,
    )


def unchanged(target, splits, body):
    if (
        target["title"],
        target["paid_by"],
        target["total_paise"],
        target["spent_on"],
        target["split_type"],
    ) != (body.title, body.paid_by, body.total_paise, body.spent_on, body.split.type):
        return False
    stored = {row["member_id"]: row for row in splits}
    if set(stored) != {member.id for member in body.split.members}:
        return False
    if body.split.type == "SHARES":
        return all(stored[member.id]["weight"] == member.weight for member in body.split.members)
    if body.split.type == "EXACT":
        return all(
            stored[member.id]["share_paise"] == parse_rupees(member.amount)
            for member in body.split.members
        )
    return True


def stored_deltas(target, splits):
    return expense_deltas(
        target["paid_by"],
        target["total_paise"],
        {row["member_id"]: row["share_paise"] for row in splits},
    )


async def edit(engine, settings, slug, expense_id, body, key, acted_by=None):
    request_hash = payload_hash("edit", slug, expense_id, canonical_body(body))

    async def operation(connection):
        room = await lock_room(connection, slug)
        replay = await lookup_idempotency(connection, room["id"], key, request_hash)
        if replay is not None:
            if acted_by is not None:
                await validate_members(connection, room["id"], {acted_by})
            return replay
        target, splits = await load_expense(connection, room, expense_id)
        if target["reversal_reason"] is not None:
            await expense_changed(connection, room, target)
        if unchanged(target, splits, body):
            if acted_by is not None:
                await validate_members(connection, room["id"], {acted_by})
            # DECISION: A no-op does not reserve its key because it creates no ledger row.
            return WriteResult({"unchanged": True, "expense_id": expense_id}, 200)
        await validate_expense(connection, room, settings, body, acted_by)
        version = await bump(connection, room, settings)
        eid = await connection.scalar(text("SELECT nextval('expenses_id_seq')"))
        shares = calculate_shares(body, eid)
        result = {
            "expense_id": eid,
            "lineage_id": target["lineage_id"],
            "replaces": expense_id,
            "graph_version": version,
            "shares": [
                {"member_id": member, "share_paise": amount}
                for member, amount in sorted(shares.items())
            ],
        }
        await connection.execute(
            text("""
            INSERT INTO reversals (group_id, version, expense_id, reason, acted_by)
            VALUES (:gid, :version, :target, 'EDIT', :actor)
        """),
            {"gid": room["id"], "version": version, "target": expense_id, "actor": acted_by},
        )
        await insert_expense(
            connection,
            room,
            body,
            key,
            acted_by,
            request_hash,
            result,
            shares,
            revision_of=expense_id,
        )
        deltas = merge(
            negate(stored_deltas(target, splits)),
            expense_deltas(body.paid_by, body.total_paise, shares),
        )
        await apply_deltas(connection, room["id"], deltas)
        return WriteResult(result, 200)

    return await write_tx(engine, operation)


async def delete(engine, settings, slug, expense_id, key, acted_by=None):
    request_hash = payload_hash("delete-expense", slug, expense_id)

    async def operation(connection):
        room = await lock_room(connection, slug)
        replay = await lookup_idempotency(connection, room["id"], key, request_hash)
        if replay is not None:
            if acted_by is not None:
                await validate_members(connection, room["id"], {acted_by})
            return replay
        target, splits = await load_expense(connection, room, expense_id)
        if target["reversal_reason"] is not None:
            await expense_changed(connection, room, target)
        if acted_by is not None:
            await validate_members(connection, room["id"], {acted_by})
        version = await bump(connection, room, settings)
        result = {
            "deleted_expense_id": expense_id,
            "lineage_id": target["lineage_id"],
            "graph_version": version,
        }
        await connection.execute(
            text("""
            INSERT INTO reversals
                (group_id, version, expense_id, reason, acted_by,
                 idempotency_key, payload_hash, response_json)
            VALUES (:gid, :version, :target, 'DELETE', :actor, :key, :hash,
                    CAST(:response AS jsonb))
        """),
            {
                "gid": room["id"],
                "version": version,
                "target": expense_id,
                "actor": acted_by,
                "key": key,
                "hash": request_hash,
                "response": json.dumps(result),
            },
        )
        await apply_deltas(connection, room["id"], negate(stored_deltas(target, splits)))
        return WriteResult(result, 200)

    return await write_tx(engine, operation)


async def restore(engine, settings, slug, expense_id, key, acted_by=None):
    request_hash = payload_hash("restore", slug, expense_id)

    async def operation(connection):
        room = await lock_room(connection, slug)
        replay = await lookup_idempotency(connection, room["id"], key, request_hash)
        if replay is not None:
            if acted_by is not None:
                await validate_members(connection, room["id"], {acted_by})
            return replay
        target, splits = await load_expense(connection, room, expense_id)
        successor = await connection.scalar(
            text("""
            SELECT id FROM expenses WHERE group_id = :gid AND revision_of = :target
        """),
            {"gid": room["id"], "target": expense_id},
        )
        if target["reversal_reason"] != "DELETE" or successor is not None:
            await expense_changed(connection, room, target)
        await check_capacity(connection, room, settings)
        if acted_by is not None:
            await validate_members(connection, room["id"], {acted_by})
        version = await bump(connection, room, settings)
        eid = await connection.scalar(text("SELECT nextval('expenses_id_seq')"))
        result = {
            "expense_id": eid,
            "lineage_id": target["lineage_id"],
            "restores": expense_id,
            "graph_version": version,
        }
        await connection.execute(
            text("""
            INSERT INTO expenses
                (id, group_id, version, lineage_id, revision_of, paid_by, acted_by,
                 title, spent_on, total_paise, split_type,
                 idempotency_key, payload_hash, response_json)
            SELECT :eid, group_id, :version, lineage_id, id, paid_by, :actor,
                   title, spent_on, total_paise, split_type, :key, :hash, CAST(:response AS jsonb)
            FROM expenses WHERE group_id = :gid AND id = :target
        """),
            {
                "eid": eid,
                "gid": room["id"],
                "version": version,
                "target": expense_id,
                "actor": acted_by,
                "key": key,
                "hash": request_hash,
                "response": json.dumps(result),
            },
        )
        await connection.execute(
            text("""
            INSERT INTO expense_splits (group_id, expense_id, member_id, weight, share_paise)
            SELECT group_id, :eid, member_id, weight, share_paise FROM expense_splits
            WHERE group_id = :gid AND expense_id = :target
        """),
            {"eid": eid, "gid": room["id"], "target": expense_id},
        )
        await apply_deltas(connection, room["id"], stored_deltas(target, splits))
        return WriteResult(result)

    return await write_tx(engine, operation)
