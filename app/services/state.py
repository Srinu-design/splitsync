from collections import OrderedDict
from weakref import WeakKeyDictionary

import anyio
from sqlalchemy import text

from app.domain.ledger import integrity_report
from app.domain.simplify import pairwise_edges, plans
from app.services.shared import limits, read_room

# DECISION: Each engine owns its cache, avoiding collisions across apps and test databases.
_caches = WeakKeyDictionary()

INTEGRITY_SQL = """-- 1. cached vs ledger, per member
WITH paid AS (
  SELECT paid_by AS member_id, sum(total_paise)::bigint AS amt
  FROM active_expenses WHERE group_id = :gid GROUP BY paid_by
), owed AS (
  SELECT s.member_id, sum(s.share_paise)::bigint AS amt
  FROM expense_splits s JOIN active_expenses e ON e.id = s.expense_id
  WHERE s.group_id = :gid GROUP BY s.member_id
), sent AS (
  SELECT from_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY from_member
), recv AS (
  SELECT to_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY to_member
)
SELECT m.id, m.net_balance_paise AS cached,
       COALESCE(paid.amt, 0) - COALESCE(owed.amt, 0) + COALESCE(sent.amt, 0) - COALESCE(recv.amt, 0) AS ledger
FROM members m
LEFT JOIN paid ON paid.member_id = m.id
LEFT JOIN owed ON owed.member_id = m.id
LEFT JOIN sent ON sent.member_id = m.id
LEFT JOIN recv ON recv.member_id = m.id
WHERE m.group_id = :gid
ORDER BY m.id;

"""

BAD_SPLITS_SQL = """-- 2. versions whose splits don't add up (must be 0)
SELECT count(*) FROM (
  SELECT e.id FROM expenses e LEFT JOIN expense_splits s ON s.expense_id = e.id
  WHERE e.group_id = :gid
  GROUP BY e.id, e.total_paise
  HAVING COALESCE(sum(s.share_paise), 0) <> e.total_paise) x;

"""

ORPHAN_EDITS_SQL = """-- 3. edit reversals without their new version (must be 0)
SELECT count(*) FROM reversals r
WHERE r.group_id = :gid AND r.reason = 'EDIT'
  AND NOT EXISTS (SELECT 1 FROM expenses n WHERE n.revision_of = r.expense_id AND n.version = r.version);
"""

OWED_SQL = """-- owed[(debtor, creditor)]: the debtor's shares in active expenses the creditor paid
SELECT s.member_id AS debtor, e.paid_by AS creditor, sum(s.share_paise)::bigint AS amount
FROM expense_splits s JOIN active_expenses e ON e.id = s.expense_id
WHERE s.group_id = :gid AND s.member_id <> e.paid_by AND s.share_paise > 0
GROUP BY s.member_id, e.paid_by;
"""


def edge_json(edge):
    return {"from": edge[0], "to": edge[1], "amount_paise": edge[2]}


def etag(version):
    return f'"v{version}"'


async def build_snapshot(engine, settings, slug):
    return (await load_snapshot(engine, settings, slug))[1]


async def load_snapshot(engine, settings, slug, if_none_match=None):
    """Return (group id, snapshot); the snapshot is None when if_none_match is the current ETag."""
    cache = _caches.setdefault(engine, OrderedDict())
    # DECISION: One read-only repeatable-read snapshot prevents torn data poisoning (gid, version).
    async with engine.begin() as connection:
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        room = await read_room(connection, slug)
        gid, version = room["id"], room["graph_version"]
        # A poll whose version is current needs only the room row, not the whole build.
        if if_none_match == etag(version):
            return gid, None
        key = (gid, version)
        if key in cache:
            cache.move_to_end(key)
            return gid, cache[key]
        members = [
            dict(row)
            for row in (
                await connection.execute(
                    text("""
            SELECT id, name, color, net_balance_paise FROM members
            WHERE group_id = :gid ORDER BY id
        """),
                    {"gid": gid},
                )
            ).mappings()
        ]
        expenses = [
            dict(row)
            for row in (
                await connection.execute(
                    text("""
            SELECT id, lineage_id, title, paid_by, total_paise, split_type, spent_on,
                   (revision_of IS NOT NULL) AS revised, acted_by
            FROM active_expenses WHERE group_id = :gid ORDER BY version DESC, id DESC LIMIT 50
        """),
                    {"gid": gid},
                )
            ).mappings()
        ]
        split_rows = (
            await connection.execute(
                text("""
            SELECT expense_id, member_id, share_paise FROM expense_splits
            WHERE group_id = :gid AND expense_id = ANY(:ids) ORDER BY expense_id, member_id
        """),
                {"gid": gid, "ids": [expense["id"] for expense in expenses]},
            )
        ).all()
        shares = {expense["id"]: [] for expense in expenses}
        for expense_id, member_id, share_paise in split_rows:
            shares[expense_id].append({"member_id": member_id, "share_paise": share_paise})
        for expense in expenses:
            expense["shares"] = shares[expense["id"]]
            expense["spent_on"] = expense["spent_on"].isoformat()
        settlements = [
            dict(row)
            for row in (
                await connection.execute(
                    text("""
            SELECT id, from_member, to_member, amount_paise, mode, acted_by, created_at
            FROM active_settlements WHERE group_id = :gid ORDER BY version DESC, id DESC LIMIT 50
        """),
                    {"gid": gid},
                )
            ).mappings()
        ]
        for settlement in settlements:
            settlement["created_at"] = settlement["created_at"].isoformat()
        owed = {
            (debtor, creditor): amount
            for debtor, creditor, amount in (
                await connection.execute(text(OWED_SQL), {"gid": gid})
            ).all()
        }
        ledger_rows = (await connection.execute(text(INTEGRITY_SQL), {"gid": gid})).all()
        bad_splits = await connection.scalar(text(BAD_SPLITS_SQL), {"gid": gid})
        orphan_edits = await connection.scalar(text(ORPHAN_EDITS_SQL), {"gid": gid})
        expense_count = await connection.scalar(
            text("SELECT count(*) FROM expenses WHERE group_id=:gid"), {"gid": gid}
        )
        settlement_count = await connection.scalar(
            text("SELECT count(*) FROM settlements WHERE group_id=:gid"), {"gid": gid}
        )
    balances = {member["id"]: member["net_balance_paise"] for member in members}
    graph = await anyio.to_thread.run_sync(plans, balances, settings.EXACT_MAX_NONZERO)
    for plan in (graph["greedy"], graph["optimal"]):
        if plan is not None:
            plan["edges"] = [edge_json(edge) for edge in plan["edges"]]
    edges = sorted(pairwise_edges(balances, owed), key=lambda edge: (-edge[2], edge[0], edge[1]))
    graph["pairwise"] = {
        "count": len(edges),
        "truncated": len(edges) > 200,
        "edges": [edge_json(edge) for edge in edges[:200]],
    }
    if room["kind"] == "DEMO":
        recommended = graph["optimal"] if graph["optimal"] is not None else graph["greedy"]
        if recommended["edges"]:
            graph["chaos_target"] = min(
                recommended["edges"],
                key=lambda edge: (-edge["amount_paise"], edge["from"], edge["to"]),
            )
    snapshot = {
        "room": {
            "slug": room["slug"],
            "kind": room["kind"],
            "name": room["name"],
            "graph_version": version,
            "expires_at": room["expires_at"].isoformat(),
        },
        "members": members,
        "expenses": expenses,
        "settlements": settlements,
        "graph": graph,
        "invariants": integrity_report(ledger_rows, bad_splits, orphan_edits),
        "limits": limits(room, settings)._asdict(),
        "usage": {
            "members": len(members),
            "expense_rows": expense_count,
            "settlements": settlement_count,
        },
    }
    cache[key] = snapshot
    cache.move_to_end(key)
    if len(cache) > 256:
        cache.popitem(last=False)
    return gid, snapshot
