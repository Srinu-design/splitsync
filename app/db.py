from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings

engine = create_async_engine(get_settings().DATABASE_URL, pool_size=5, max_overflow=5)
# DECISION: Keep loaded values usable after commit; callers own session and transaction lifetimes.
session_factory = async_sessionmaker(engine, expire_on_commit=False)
