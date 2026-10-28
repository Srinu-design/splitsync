from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy.exc import DBAPIError

from app.errors import ApiError
from app.schemas import SettlementIn
from app.services.shared import WriteResult


class PgError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate


def database_error(state):
    return DBAPIError("test statement", {}, PgError(state))


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:dev@localhost:5433/unused")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6380/0")
    from app import db

    return db


@pytest.fixture
def transaction(monkeypatch, db):
    connection = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.begin.return_value = context
    sleep = AsyncMock()
    monkeypatch.setattr(db.asyncio, "sleep", sleep)
    monkeypatch.setattr(db.random, "uniform", Mock(return_value=0.125))
    return engine, connection, context, sleep


@pytest.mark.parametrize("state", ["40001", "40P01"])
@pytest.mark.parametrize("failure_at", ["operation", "commit"])
async def test_write_retries_entire_transaction_with_local_timeouts(
    transaction, db, state, failure_at
):
    engine, connection, context, sleep = transaction
    operation = AsyncMock(return_value="committed")
    if failure_at == "operation":
        operation.side_effect = [database_error(state), database_error(state), "committed"]
    else:
        context.__aexit__.side_effect = [database_error(state), database_error(state), False]
    assert await db.write_tx(engine, operation) == "committed"
    assert engine.begin.call_count == operation.await_count == 3
    assert sleep.await_count == 2
    assert all(call.args == (0.125,) for call in sleep.await_args_list)
    assert all(call.args == (0.05, 0.2) for call in db.random.uniform.call_args_list)
    assert [str(call.args[0]) for call in connection.execute.await_args_list] == [
        "SET LOCAL lock_timeout = '2s'",
        "SET LOCAL statement_timeout = '5s'",
    ] * 3


@pytest.mark.parametrize("state", ["40001", "40P01"])
async def test_write_stops_after_three_attempts(transaction, db, state):
    engine, _, _, sleep = transaction
    operation = AsyncMock(side_effect=database_error(state))
    with pytest.raises(DBAPIError):
        await db.write_tx(engine, operation)
    assert engine.begin.call_count == operation.await_count == 3
    assert sleep.await_count == 2


async def test_lock_timeout_maps_to_busy_without_retry(transaction, db):
    engine, _, _, sleep = transaction
    operation = AsyncMock(side_effect=database_error("55P03"))
    with pytest.raises(ApiError) as error:
        await db.write_tx(engine, operation)
    assert error.value.status == 409
    assert error.value.code == "GROUP_BUSY"
    assert error.value.headers == {"Retry-After": "1"}
    assert operation.await_count == 1
    sleep.assert_not_awaited()


async def test_other_database_errors_are_not_retried(transaction, db):
    engine, _, _, sleep = transaction
    operation = AsyncMock(side_effect=database_error("23505"))
    with pytest.raises(DBAPIError):
        await db.write_tx(engine, operation)
    assert operation.await_count == 1
    sleep.assert_not_awaited()


async def test_naive_postcommit_bump_failure_never_repeats_the_ledger_write(
    transaction, monkeypatch
):
    from app.services import settlements

    engine, connection, _, sleep = transaction
    writer = AsyncMock(return_value=(WriteResult({"settlement_id": 1}), 123))
    monkeypatch.setattr(settlements, "write_tx", writer)
    connection.execute.side_effect = database_error("40001")
    body = SettlementIn(
        from_member=5, to_member=1, amount="5000", expected_graph_version=9, mode="NAIVE"
    )
    with pytest.raises(DBAPIError):
        await settlements.create(engine, MagicMock(), "room0001", body, "naive-bump-failure")
    writer.assert_awaited_once()
    assert str(connection.execute.await_args.args[0]).strip().startswith("UPDATE groups")
    sleep.assert_not_awaited()
