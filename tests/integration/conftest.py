import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


@pytest.fixture(scope="session")
def migrated_database():
    url = os.getenv(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5433/splitsync_test"
    )
    # DECISION: Refuse destructive schema tests unless the database is explicitly splitsync_test.
    parsed = make_url(url)
    if parsed.database != "splitsync_test" or parsed.drivername != "postgresql+asyncpg":
        raise ValueError(
            "TEST_DATABASE_URL must use postgresql+asyncpg and database splitsync_test"
        )
    env = {**os.environ, "DATABASE_URL": url}
    env.setdefault("REDIS_URL", "redis://localhost:6380/0")

    def migrate(action, revision):
        subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    migrate("upgrade", "head")
    return url, migrate


@pytest_asyncio.fixture
async def engine(migrated_database):
    url, _ = migrated_database
    # DECISION: Test connections use NullPool so none cross pytest's per-test event loops.
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE groups RESTART IDENTITY CASCADE"))
        yield engine
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE groups RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()
