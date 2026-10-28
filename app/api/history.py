from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.rate_limit import require_rate_limit
from app.services import history

router = APIRouter(dependencies=[Depends(require_rate_limit("read"))])


@router.get("/api/rooms/{slug}/activity")
async def list_activity(
    request: Request,
    slug: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    lineage: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
):
    return await history.list_activity(
        request.app.state.engine,
        request.app.state.settings,
        slug,
        limit=limit,
        cursor=cursor,
        lineage=lineage,
    )
