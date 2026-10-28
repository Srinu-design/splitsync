import re
from datetime import date

from sqlalchemy import text

from app.domain.money import InvalidAmount, parse_rupees
from app.errors import ApiError
from app.services import shared

SEARCH_SQL = """WITH q AS (
  SELECT CASE WHEN cardinality(CAST(:tokens AS text[])) = 0 THEN NULL
              ELSE to_tsquery('simple', (SELECT string_agg(quote_literal(t) || ':*', ' & ')
                                         FROM unnest(CAST(:tokens AS text[])) AS t))
         END AS tsq
)
SELECT e.id, e.lineage_id, e.title, e.total_paise, e.paid_by, e.spent_on,
       (r.id IS NOT NULL) AS deleted, (e.revision_of IS NOT NULL) AS revised
FROM expenses e
LEFT JOIN reversals r ON r.expense_id = e.id
CROSS JOIN q
WHERE e.group_id = :gid
  AND (q.tsq IS NULL OR e.title_tsv @@ q.tsq)
  AND (r.id IS NULL OR (:include_deleted AND r.reason = 'DELETE'
                        AND NOT EXISTS (SELECT 1 FROM expenses n WHERE n.revision_of = e.id)))
  AND (CAST(:paid_by AS bigint) IS NULL OR e.paid_by = :paid_by)
  AND (CAST(:member AS bigint) IS NULL OR EXISTS (SELECT 1 FROM expense_splits s
                                                  WHERE s.expense_id = e.id AND s.member_id = :member))
  AND (CAST(:min AS bigint) IS NULL OR e.total_paise >= :min)
  AND (CAST(:max AS bigint) IS NULL OR e.total_paise <= :max)
  AND (CAST(:date_from AS date) IS NULL OR e.spent_on >= :date_from)
  AND (CAST(:date_to AS date) IS NULL OR e.spent_on <= :date_to)
  AND (e.spent_on, e.id) < (CAST(:cursor_date AS date), CAST(:cursor_id AS bigint))
ORDER BY e.spent_on DESC, e.id DESC
LIMIT :lim;
"""

MAX_BIGINT = 2**63 - 1


def decode_cursor(cursor: str | None) -> tuple[date, int]:
    if cursor is None:
        return date.max, MAX_BIGINT
    try:
        # DECISION: Use the feed's 128-character cursor bound before parsing.
        if not isinstance(cursor, str) or len(cursor) > 128:
            raise ValueError
        match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})_([1-9][0-9]{0,18})", cursor)
        if match is None:
            raise ValueError
        spent_on = date.fromisoformat(match[1])
        row_id = int(match[2])
        if row_id > MAX_BIGINT:
            raise ValueError
        return spent_on, row_id
    except ValueError as exc:
        raise ApiError(
            400, "INVALID_CURSOR", "Invalid cursor", "Use a cursor from this search."
        ) from exc


def search_tokens(q: str) -> list[str]:
    try:
        if not isinstance(q, str) or len(q) > 1000 or "\x00" in q:
            raise ValueError
        q.encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ApiError(
            422, "VALIDATION_ERROR", "Invalid search", "Enter valid search text."
        ) from exc
    # DECISION: Keep the first eight word tokens and truncate each to forty characters.
    return [token[:40] for token in re.findall(r"\w+", q.lower())[:8]]


def query_date(value) -> date | None:
    if value is None or type(value) is date:
        return value
    try:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(
            422, "VALIDATION_ERROR", "Invalid date", "Use an ISO date: YYYY-MM-DD."
        ) from exc


def query_amount(value) -> int | None:
    if value is None:
        return None
    try:
        return parse_rupees(value)
    except InvalidAmount as exc:
        raise ApiError(
            422, "INVALID_AMOUNT", "Invalid amount", "Enter a valid rupee amount."
        ) from exc


async def find_expenses(
    engine,
    settings,
    slug,
    *,
    q="",
    paid_by=None,
    member=None,
    min_amount=None,
    max_amount=None,
    date_from=None,
    date_to=None,
    include_deleted=False,
    cursor=None,
    limit=50,
) -> dict:
    cursor_date, cursor_id = decode_cursor(cursor)
    parameters = {
        "tokens": search_tokens(q),
        "paid_by": paid_by,
        "member": member,
        "min": query_amount(min_amount),
        "max": query_amount(max_amount),
        "date_from": query_date(date_from),
        "date_to": query_date(date_to),
        "include_deleted": include_deleted,
        "cursor_date": cursor_date,
        "cursor_id": cursor_id,
        "lim": limit + 1,
    }
    async with engine.begin() as connection:
        room = await shared.read_room(connection, slug)
        rows = (
            (await connection.execute(text(SEARCH_SQL), {"gid": room["id"], **parameters}))
            .mappings()
            .all()
        )
    page = rows[:limit]
    items = [
        {
            "expense_id": row["id"],
            **{
                key: row[key]
                for key in ("lineage_id", "title", "total_paise", "paid_by", "deleted", "revised")
            },
            "spent_on": row["spent_on"].isoformat(),
        }
        for row in page
    ]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = f"{last['spent_on'].isoformat()}_{last['id']}"
    return {"items": items, "next_cursor": next_cursor}
