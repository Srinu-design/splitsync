from fastapi import APIRouter, Request

from app.api import ActingMember, response
from app.schemas import RoomIn
from app.services import rooms

router = APIRouter()


@router.post("/api/rooms", status_code=201)
async def create(request: Request, body: RoomIn, acted_by: ActingMember = None):
    return response(
        await rooms.create(request.app.state.engine, request.app.state.settings, body, acted_by)
    )
