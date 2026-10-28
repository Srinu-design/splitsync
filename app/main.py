import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.api import expenses, health, history, members, rooms, search, settlements, state, ws
from app.config import get_settings
from app.errors import register_error_handlers
from app.redis_client import make_redis
from app.services import realtime
from app.services import rooms as room_service
from app.services.realtime import ConnectionManager


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
    register_error_handlers(application)
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
    return application


app = create_app()
