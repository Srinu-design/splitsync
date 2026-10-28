from fastapi import APIRouter, Request

from app.api import ActingMember, response
from app.schemas import MemberIn
from app.services import members

router = APIRouter()


@router.post("/api/rooms/{slug}/members", status_code=201)
async def create(request: Request, slug: str, body: MemberIn, acted_by: ActingMember = None):
    return response(
        await members.create(
            request.app.state.engine, request.app.state.settings, slug, body, acted_by
        )
    )
