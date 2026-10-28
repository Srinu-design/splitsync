import base64
import binascii
import re

from sqlalchemy import text

from app.errors import ApiError
from app.services import shared

HISTORY_SQL = """SELECT version, rank, id, kind, data FROM (
  (SELECT e.version, 3 AS rank, e.id, 'EXPENSE' AS kind,
          jsonb_build_object('title', e.title, 'total_paise', e.total_paise, 'paid_by', e.paid_by,
                             'lineage_id', e.lineage_id, 'revision_of', e.revision_of, 'parent_reason', pr.reason,
                             'old_title', p.title, 'old_total_paise', p.total_paise, 'spent_on', e.spent_on,
                             'acted_by', e.acted_by, 'at', e.created_at) AS data
   FROM expenses e
   LEFT JOIN expenses p ON p.id = e.revision_of
   LEFT JOIN reversals pr ON pr.expense_id = e.revision_of
   WHERE e.group_id = :gid AND (e.version, 3, e.id) < (:cv, :ck, :cid)
     AND (CAST(:lineage AS bigint) IS NULL OR e.lineage_id = :lineage)
   ORDER BY e.version DESC, e.id DESC LIMIT :lim)
  UNION ALL
  (SELECT s.version, 2, s.id, 'SETTLEMENT',
          jsonb_build_object('from', s.from_member, 'to', s.to_member, 'amount_paise', s.amount_paise,
                             'mode', s.mode, 'acted_by', s.acted_by, 'at', s.created_at)
   FROM settlements s
   WHERE s.group_id = :gid AND (s.version, 2, s.id) < (:cv, :ck, :cid) AND CAST(:lineage AS bigint) IS NULL
   ORDER BY s.version DESC, s.id DESC LIMIT :lim)
  UNION ALL
  (SELECT r.version, 1, r.id, 'REVERSAL',
          jsonb_build_object('expense_id', r.expense_id, 'settlement_id', r.settlement_id,
                             'title', x.title, 'total_paise', x.total_paise, 'lineage_id', x.lineage_id,
                             'from', st.from_member, 'to', st.to_member, 'amount_paise', st.amount_paise,
                             'acted_by', r.acted_by, 'at', r.created_at)
   FROM reversals r
   LEFT JOIN expenses x ON x.id = r.expense_id
   LEFT JOIN settlements st ON st.id = r.settlement_id
   WHERE r.group_id = :gid AND r.reason = 'DELETE' AND (r.version, 1, r.id) < (:cv, :ck, :cid)
     AND (CAST(:lineage AS bigint) IS NULL OR x.lineage_id = :lineage)
   ORDER BY r.version DESC, r.id DESC LIMIT :lim)
  UNION ALL
  (SELECT m.version, 0, m.id, 'MEMBER', jsonb_build_object('name', m.name, 'at', m.created_at)
   FROM members m
   WHERE m.group_id = :gid AND (m.version, 0, m.id) < (:cv, :ck, :cid) AND CAST(:lineage AS bigint) IS NULL
   ORDER BY m.version DESC, m.id DESC LIMIT :lim)
) a
ORDER BY version DESC, rank DESC, id DESC
LIMIT :lim;
"""

MAX_BIGINT = 2**63 - 1


def encode_cursor(version: int, rank: int, row_id: int) -> str:
    return (
        base64.urlsafe_b64encode(f"{version}.{rank}.{row_id}".encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_cursor(cursor: str | None) -> tuple[int, int, int]:
    if cursor is None:
        return MAX_BIGINT, 9, MAX_BIGINT
    try:
        # DECISION: Bound cursors to 128 characters before decoding untrusted input.
        if not isinstance(cursor, str) or len(cursor) > 128:
            raise ValueError
        if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", cursor) is None:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        decoded = raw.decode("ascii")
        match = re.fullmatch(r"([1-9][0-9]{0,18})\.([0-3])\.([1-9][0-9]{0,18})", decoded)
        if match is None:
            raise ValueError
        version, rank, row_id = map(int, match.groups())
        if version > MAX_BIGINT or row_id > MAX_BIGINT:
            raise ValueError
        if encode_cursor(version, rank, row_id) != cursor.rstrip("="):
            raise ValueError
        return version, rank, row_id
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ApiError(
            400, "INVALID_CURSOR", "Invalid cursor", "Use a cursor from this feed."
        ) from exc


def activity_item(row) -> dict:
    data = row["data"]
    item = {"version": row["version"], "at": data["at"], "acted_by": data.get("acted_by")}
    if row["kind"] == "EXPENSE":
        item.update(
            expense_id=row["id"],
            **{
                key: data[key]
                for key in ("lineage_id", "title", "total_paise", "paid_by", "spent_on")
            },
        )
        if data["revision_of"] is None:
            item["type"] = "EXPENSE_ADDED"
        elif data["parent_reason"] == "EDIT":
            item.update(
                type="EXPENSE_EDITED",
                replaces=data["revision_of"],
                old_title=data["old_title"],
                old_total_paise=data["old_total_paise"],
            )
        else:
            item.update(type="EXPENSE_RESTORED", restores=data["revision_of"])
    elif row["kind"] == "SETTLEMENT":
        item.update(
            type="SETTLEMENT_RECORDED",
            settlement_id=row["id"],
            **{key: data[key] for key in ("from", "to", "amount_paise", "mode")},
        )
    elif row["kind"] == "REVERSAL" and data["expense_id"] is not None:
        item.update(
            type="EXPENSE_DELETED",
            **{key: data[key] for key in ("expense_id", "lineage_id", "title", "total_paise")},
        )
    elif row["kind"] == "REVERSAL":
        item.update(
            type="SETTLEMENT_DELETED",
            **{key: data[key] for key in ("settlement_id", "from", "to", "amount_paise")},
        )
    else:
        item.update(type="MEMBER_ADDED", member_id=row["id"], name=data["name"])
    return item


async def list_activity(engine, settings, slug, *, limit=50, cursor=None, lineage=None) -> dict:
    version, rank, row_id = decode_cursor(cursor)
    async with engine.begin() as connection:
        room = await shared.read_room(connection, slug)
        rows = (
            (
                await connection.execute(
                    text(HISTORY_SQL),
                    {
                        "gid": room["id"],
                        "cv": version,
                        "ck": rank,
                        "cid": row_id,
                        "lineage": lineage,
                        "lim": limit + 1,
                    },
                )
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_cursor(last["version"], last["rank"], last["id"])
    return {"items": [activity_item(row) for row in page], "next_cursor": next_cursor}
