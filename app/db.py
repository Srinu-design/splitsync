import asyncio
import random
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.errors import ApiError


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
    )


engine = make_engine(get_settings())


async def write_tx[T](
    engine: AsyncEngine, operation: Callable[[AsyncConnection], Awaitable[T]]
) -> T:
    for attempt in range(3):
        try:
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL lock_timeout = '2s'"))
                await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                result = await operation(connection)
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None)
            if state == "55P03":
                raise ApiError(
                    409,
                    "GROUP_BUSY",
                    "Room is busy",
                    "Try the write again shortly.",
                    headers={"Retry-After": "1"},
                ) from error
            if state not in {"40001", "40P01"} or attempt == 2:
                raise
            await asyncio.sleep(random.uniform(0.05, 0.2))
        else:
            return result
