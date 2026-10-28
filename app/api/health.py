from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    return {"status": "ok"}


@router.api_route("/readyz", methods=["GET", "HEAD"])
async def readyz(request: Request):
    engine = request.app.state.engine
    redis_client = getattr(request.app.state, "redis", None)

    # Check database
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        # DECISION: Return 503 only when the database connection fails.
        if request.method == "HEAD":
            return Response(status_code=503)
        return JSONResponse({"db": "down"}, status_code=503)

    # Check Redis
    is_redis_ok = False
    if redis_client is not None:
        is_redis_ok = await redis_client.ping()

    if request.method == "HEAD":
        return Response(status_code=200)

    # DECISION: Report redis as degraded if Redis ping fails while keeping HTTP 200.
    redis_status = "ok" if is_redis_ok else "degraded"
    return {"db": "ok", "redis": redis_status}
