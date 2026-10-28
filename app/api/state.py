from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse

from app.services.state import build_snapshot

router = APIRouter()


@router.get("/api/rooms/{slug}/state")
async def state(
    request: Request,
    slug: str,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
):
    snapshot = await build_snapshot(request.app.state.engine, request.app.state.settings, slug)
    etag = f'"v{snapshot["room"]["graph_version"]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(snapshot, headers={"ETag": etag})
