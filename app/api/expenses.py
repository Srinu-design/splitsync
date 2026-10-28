from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api import ActingMember, idempotency_key, response
from app.schemas import ExpenseIn
from app.services import expenses

router = APIRouter()


@router.post("/api/rooms/{slug}/expenses", status_code=201)
async def create(
    request: Request,
    slug: str,
    body: ExpenseIn,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    return response(
        await expenses.create(
            request.app.state.engine, request.app.state.settings, slug, body, key, acted_by
        )
    )
