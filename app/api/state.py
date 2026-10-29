from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from app.services.state import etag, load_snapshot

router = APIRouter()


@router.get("/api/rooms/{slug}/state")
async def state(
    request: Request,
    slug: str,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
):
    _, snapshot = await load_snapshot(
        request.app.state.engine, request.app.state.settings, slug, if_none_match
    )
    if snapshot is None:
        return Response(status_code=304, headers={"ETag": if_none_match})
    return JSONResponse(snapshot, headers={"ETag": etag(snapshot["room"]["graph_version"])})
