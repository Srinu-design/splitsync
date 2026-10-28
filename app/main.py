import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import expenses, health, history, members, rooms, search, settlements, state, ws
from app.config import get_settings
from app.errors import register_error_handlers
from app.logging import setup_logging
from app.middleware import BodyLimitMiddleware, ObservabilityMiddleware
from app.redis_client import make_redis
from app.services import realtime
from app.services import rooms as room_service
from app.services.realtime import ConnectionManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app):
    settings = app.state.settings
    redis_client = make_redis(settings.REDIS_URL)
    app.state.redis = redis_client

    manager = ConnectionManager(settings)
    app.state.manager = manager

    listener_task = asyncio.create_task(realtime.redis_listener(redis_client, manager))
    sweep_task = asyncio.create_task(
        room_service.sweep_loop(app.state.engine, settings, redis_client)
    )

    try:
        yield
    finally:
        listener_task.cancel()
        sweep_task.cancel()
        await asyncio.gather(listener_task, sweep_task, return_exceptions=True)
        await redis_client.aclose()
        await app.state.engine.dispose()


def create_app(settings=None):
    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.settings = settings if settings is not None else get_settings()
    application.state.engine = db.make_engine(settings) if settings is not None else db.engine
    # Pre-initialize manager and redis on state for tests that don't invoke lifespan
    application.state.manager = ConnectionManager(application.state.settings)
    application.state.redis = make_redis(application.state.settings.REDIS_URL)
    setup_logging(application.state.settings.LOG_LEVEL)
    register_error_handlers(application)

    application.add_middleware(BodyLimitMiddleware)
    application.add_middleware(ObservabilityMiddleware)

    # DECISION: Middleware adds CSP, X-Content-Type-Options, and Referrer-Policy to every HTTP
    # response because the shared host's proxy does not add them.
    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Revalidate pages and assets (cheap with the ETag) so a deploy shows at once.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    for router in (
        rooms.router,
        members.router,
        expenses.router,
        settlements.router,
        state.router,
        history.router,
        search.router,
        ws.router,
        health.router,
    ):
        application.include_router(router)

    # DECISION: Serve static pages at / and /r/{slug} with GET and HEAD support without
    # database access; the client JS renders state or 404.
    @application.api_route("/", methods=["GET", "HEAD"], response_class=FileResponse)
    async def get_index(request: Request):
        if request.method == "HEAD":
            return Response(status_code=200, media_type="text/html")
        return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html")

    @application.api_route("/r/{slug}", methods=["GET", "HEAD"], response_class=FileResponse)
    async def get_room(request: Request, slug: str):
        if request.method == "HEAD":
            return Response(status_code=200, media_type="text/html")
        return FileResponse(str(STATIC_DIR / "room.html"), media_type="text/html")

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return application


app = create_app()
