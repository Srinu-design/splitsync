from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api import ActingMember, response
from app.schemas import RoomIn
from app.services import rooms

router = APIRouter()


@router.post("/api/rooms", status_code=201)
async def create(request: Request, body: RoomIn, acted_by: ActingMember = None):
    return response(
        await rooms.create(request.app.state.engine, request.app.state.settings, body, acted_by)
    )


@router.post("/api/rooms/{slug}/reset")
async def reset(request: Request, slug: str, acted_by: ActingMember = None):
    return JSONResponse(
        await rooms.reset(request.app.state.engine, request.app.state.settings, slug, acted_by)
    )
