from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    engine = request.app.state.engine
    redis_client = getattr(request.app.state, "redis", None)

    # Check database
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        # DECISION: Return 503 only when the database connection fails.
        return JSONResponse({"db": "down"}, status_code=503)

    # Check Redis
    is_redis_ok = False
    if redis_client is not None:
        is_redis_ok = await redis_client.ping()

    # DECISION: Report redis as degraded if Redis ping fails while keeping HTTP 200.
    redis_status = "ok" if is_redis_ok else "degraded"
    return {"db": "ok", "redis": redis_status}
