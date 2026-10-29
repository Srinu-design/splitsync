import re
from typing import Annotated

from fastapi import Header, Path
from fastapi.responses import JSONResponse

from app.errors import ApiError
from app.services.realtime import publish_snapshot_task

ActingMember = Annotated[int | None, Header(alias="X-Acting-Member", ge=1, le=2**63 - 1)]
TargetId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if value is None or re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value) is None:
        raise ApiError(
            400,
            "IDEMPOTENCY_KEY_MISSING",
            "Idempotency key required",
            "Send an Idempotency-Key with 16–64 letters, digits, underscores or hyphens.",
        )
    return value


def response(result):
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


def schedule_publish(background_tasks, app, slug: str, result) -> None:
    # DECISION: Replays and unchanged edits skip background snapshot publishing.
    replayed = (result.headers or {}).get("Idempotent-Replayed") == "true"
    if not replayed and result.body.get("unchanged") is not True:
        background_tasks.add_task(publish_snapshot_task, app, slug)
