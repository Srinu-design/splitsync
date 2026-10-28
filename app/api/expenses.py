from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.api import ActingMember, TargetId, idempotency_key, response, schedule_publish
from app.schemas import ExpenseIn
from app.services import expenses

router = APIRouter()


@router.post("/api/rooms/{slug}/expenses", status_code=201)
async def create(
    request: Request,
    slug: str,
    body: ExpenseIn,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    res = await expenses.create(
        request.app.state.engine, request.app.state.settings, slug, body, key, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)


@router.put("/api/rooms/{slug}/expenses/{expense_id}")
async def edit(
    request: Request,
    slug: str,
    expense_id: TargetId,
    body: ExpenseIn,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    res = await expenses.edit(
        request.app.state.engine,
        request.app.state.settings,
        slug,
        expense_id,
        body,
        key,
        acted_by,
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)


@router.delete("/api/rooms/{slug}/expenses/{expense_id}")
async def delete(
    request: Request,
    slug: str,
    expense_id: TargetId,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    res = await expenses.delete(
        request.app.state.engine, request.app.state.settings, slug, expense_id, key, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)


@router.post("/api/rooms/{slug}/expenses/{expense_id}/restore", status_code=201)
async def restore(
    request: Request,
    slug: str,
    expense_id: TargetId,
    background_tasks: BackgroundTasks,
    key: Annotated[str, Depends(idempotency_key)],
    acted_by: ActingMember = None,
):
    res = await expenses.restore(
        request.app.state.engine, request.app.state.settings, slug, expense_id, key, acted_by
    )
    schedule_publish(background_tasks, request.app, slug, res)
    return response(res)
