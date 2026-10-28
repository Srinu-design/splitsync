from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.api import ActingMember, TargetId, idempotency_key, response, schedule_publish
from app.api.rate_limit import enforce_rate_limit, require_rate_limit
from app.schemas import SettlementIn
from app.services import settlements

router = APIRouter(dependencies=[Depends(require_rate_limit("write"))])


@router.post("/api/rooms/{slug}/settlements", status_code=201)
async def create(
    request: Request,
    slug: str,
    body: SettlementIn,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    if body.mode == "NAIVE":
        await enforce_rate_limit(request, "naive")
    res = await settlements.create(
        request.app.state.engine, request.app.state.settings, slug, body, key, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)


@router.delete("/api/rooms/{slug}/settlements/{settlement_id}")
async def delete(
    request: Request,
    slug: str,
    settlement_id: TargetId,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    res = await settlements.delete(
        request.app.state.engine, request.app.state.settings, slug, settlement_id, key, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)
