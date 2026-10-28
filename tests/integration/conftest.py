import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app


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


INTEGRITY_SQL = """-- 1. cached vs ledger, per member
WITH paid AS (
  SELECT paid_by AS member_id, sum(total_paise)::bigint AS amt
  FROM active_expenses WHERE group_id = :gid GROUP BY paid_by
), owed AS (
  SELECT s.member_id, sum(s.share_paise)::bigint AS amt
  FROM expense_splits s JOIN active_expenses e ON e.id = s.expense_id
  WHERE s.group_id = :gid GROUP BY s.member_id
), sent AS (
  SELECT from_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY from_member
), recv AS (
  SELECT to_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY to_member
)
SELECT m.id, m.net_balance_paise AS cached,
       COALESCE(paid.amt, 0) - COALESCE(owed.amt, 0) + COALESCE(sent.amt, 0) - COALESCE(recv.amt, 0) AS ledger
FROM members m
LEFT JOIN paid ON paid.member_id = m.id
LEFT JOIN owed ON owed.member_id = m.id
LEFT JOIN sent ON sent.member_id = m.id
LEFT JOIN recv ON recv.member_id = m.id
WHERE m.group_id = :gid
ORDER BY m.id;

"""


BAD_SPLITS_SQL = """-- 2. versions whose splits don't add up (must be 0)
SELECT count(*) FROM (
  SELECT e.id FROM expenses e LEFT JOIN expense_splits s ON s.expense_id = e.id
  WHERE e.group_id = :gid
  GROUP BY e.id, e.total_paise
  HAVING COALESCE(sum(s.share_paise), 0) <> e.total_paise) x;

"""

ORPHAN_EDITS_SQL = """-- 3. edit reversals without their new version (must be 0)
SELECT count(*) FROM reversals r
WHERE r.group_id = :gid AND r.reason = 'EDIT'
  AND NOT EXISTS (SELECT 1 FROM expenses n WHERE n.revision_of = r.expense_id AND n.version = r.version);
"""


@pytest.fixture
async def app(engine, migrated_database):
    url, _ = migrated_database
    settings = Settings(
        _env_file=None,
        DATABASE_URL=url,
        REDIS_URL="redis://localhost:6380/0",
        DB_POOL_SIZE=12,
        DB_MAX_OVERFLOW=0,
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        # DECISION: Prewarm the real pool so ten naive reads overlap within the specified 150 ms.
        connections = await asyncio.gather(*(application.state.engine.connect() for _ in range(12)))
        await asyncio.gather(*(connection.close() for connection in connections))
        yield application


@pytest.fixture
async def integrity(engine):
    async def report(slug):
        async with engine.connect() as connection:
            gid = await connection.scalar(
                text("SELECT id FROM groups WHERE slug=:slug"), {"slug": slug}
            )
            rows = (await connection.execute(text(INTEGRITY_SQL), {"gid": gid})).all()
            bad_splits = await connection.scalar(text(BAD_SPLITS_SQL), {"gid": gid})
            orphan_edits = await connection.scalar(text(ORPHAN_EDITS_SQL), {"gid": gid})
        return {
            "bad_split_expenses": bad_splits,
            "orphan_edits": orphan_edits,
            "zero_sum_paise": sum(cached for _, cached, _ in rows),
            "drift": {member: cached - ledger for member, cached, ledger in rows},
            "cached": {member: cached for member, cached, _ in rows},
            "ledger": {member: ledger for member, _, ledger in rows},
        }

    return report


@pytest.fixture
async def client(app, engine, integrity, request):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    async with engine.connect() as connection:
        slugs = list(await connection.scalars(text("SELECT slug FROM groups")))
    for slug in slugs:
        report = await integrity(slug)
        assert report["bad_split_expenses"] == report["orphan_edits"] == 0, report
        if request.node.get_closest_marker("naive_drift") is None:
            assert report["zero_sum_paise"] == 0, report
            assert all(drift == 0 for drift in report["drift"].values()), report


@pytest.fixture
async def demo(client, engine):
    response = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert response.status_code == 201, response.text
    slug = response.json()["slug"]
    async with engine.connect() as connection:
        members = dict(
            (
                await connection.execute(
                    text("""
            SELECT m.name, m.id FROM members m JOIN groups g ON g.id=m.group_id
            WHERE g.slug=:slug ORDER BY m.id
        """),
                    {"slug": slug},
                )
            ).all()
        )
    return {"slug": slug, "members": members}
