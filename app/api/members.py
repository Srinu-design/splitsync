from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.api import ActingMember, response, schedule_publish
from app.api.rate_limit import require_rate_limit
from app.schemas import MemberIn
from app.services import members

router = APIRouter()


@router.post(
    "/api/rooms/{slug}/members",
    status_code=201,
    dependencies=[Depends(require_rate_limit("write"))],
)
async def create(
    request: Request,
    slug: str,
    body: MemberIn,
    background_tasks: BackgroundTasks,
    acted_by: ActingMember = None,
):
    res = await members.create(
        request.app.state.engine, request.app.state.settings, slug, body, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)
