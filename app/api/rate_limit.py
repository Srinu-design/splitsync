import logging
from typing import Any

from fastapi import Request

from app.errors import ApiError

logger = logging.getLogger(__name__)

# DECISION: Scope rate limits per SPEC §14 table (scope -> (limit, window_seconds))
DEFAULT_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "create_room": (10, 3600),
    "write": (60, 60),
    "naive": (30, 60),
    "read": (120, 60),
    "ws_connect": (30, 60),
}

# Mutable copy to allow tests to patch limits dynamically
RATE_LIMITS: dict[str, tuple[int, int]] = dict(DEFAULT_RATE_LIMITS)


def get_client_ip(request: Request) -> str:
    # Client IP = request.client.host (uvicorn --proxy-headers already rewrites it)
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


async def check_rate_limit(
    redis_client: Any, settings: Any, scope: str, client_ip: str
) -> tuple[bool, int]:
    if not settings.RATE_LIMIT_ENABLED:
        return True, 0
    limit, window_seconds = RATE_LIMITS.get(scope, (60, 60))
    return await redis_client.check_rate_limit(scope, client_ip, limit, window_seconds)


async def enforce_rate_limit(request: Request, scope: str) -> None:
    settings = request.app.state.settings
    if not settings.RATE_LIMIT_ENABLED:
        return
    client_ip = get_client_ip(request)
    allowed, retry_after = await check_rate_limit(
        request.app.state.redis, settings, scope, client_ip
    )
    if not allowed:
        # DECISION: Over rate limit returns 429 RATE_LIMITED with Retry-After header.
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Too Many Requests",
            f"Rate limit exceeded for {scope}. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def require_rate_limit(scope: str):
    async def dependency(request: Request) -> None:
        await enforce_rate_limit(request, scope)

    return dependency
