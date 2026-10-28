from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api import ActingMember, idempotency_key, response
from app.schemas import SettlementIn
from app.services import settlements

router = APIRouter()


@router.post("/api/rooms/{slug}/settlements", status_code=201)
async def create(
    request: Request,
    slug: str,
    body: SettlementIn,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    return response(
        await settlements.create(
            request.app.state.engine, request.app.state.settings, slug, body, key, acted_by
        )
    )
