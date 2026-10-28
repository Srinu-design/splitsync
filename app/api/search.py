from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.rate_limit import require_rate_limit
from app.services import search

router = APIRouter(dependencies=[Depends(require_rate_limit("read"))])


@router.get("/api/rooms/{slug}/expenses")
async def find_expenses(
    request: Request,
    slug: str,
    # DECISION: Bound free-text search to 1000 characters before tokenization.
    q: Annotated[str, Query(max_length=1000)] = "",
    paid_by: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    member: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    # DECISION: Apply the existing 32-character money bound to search amount filters.
    min_amount: Annotated[str | None, Query(alias="min", max_length=32)] = None,
    max_amount: Annotated[str | None, Query(alias="max", max_length=32)] = None,
    date_from: Annotated[str | None, Query(alias="from", max_length=10)] = None,
    date_to: Annotated[str | None, Query(alias="to", max_length=10)] = None,
    include_deleted: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await search.find_expenses(
        request.app.state.engine,
        request.app.state.settings,
        slug,
        q=q,
        paid_by=paid_by,
        member=member,
        min_amount=min_amount,
        max_amount=max_amount,
        date_from=date_from,
        date_to=date_to,
        include_deleted=include_deleted,
        cursor=cursor,
        limit=limit,
    )
