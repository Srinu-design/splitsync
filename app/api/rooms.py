from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.api import ActingMember, response
from app.schemas import RoomIn
from app.services import rooms
from app.services.realtime import publish_snapshot_task

router = APIRouter()


@router.post("/api/rooms", status_code=201)
async def create(request: Request, body: RoomIn, acted_by: ActingMember = None):
    return response(
        await rooms.create(request.app.state.engine, request.app.state.settings, body, acted_by)
    )


@router.post("/api/rooms/{slug}/reset")
async def reset(
    request: Request, slug: str, background_tasks: BackgroundTasks, acted_by: ActingMember = None
):
    snapshot = await rooms.reset(
        request.app.state.engine, request.app.state.settings, slug, acted_by
    )
    background_tasks.add_task(publish_snapshot_task, request.app, slug)
    return JSONResponse(snapshot)
