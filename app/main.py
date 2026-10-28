from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.api import expenses, members, rooms, settlements
from app.config import get_settings
from app.errors import register_error_handlers


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        await app.state.engine.dispose()


def create_app(settings=None):
    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.state.settings = settings if settings is not None else get_settings()
    application.state.engine = db.make_engine(settings) if settings is not None else db.engine
    register_error_handlers(application)
    for router in (rooms.router, members.router, expenses.router, settlements.router):
        application.include_router(router)
    return application


app = create_app()
