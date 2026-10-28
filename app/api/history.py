from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.services import history

router = APIRouter()


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
