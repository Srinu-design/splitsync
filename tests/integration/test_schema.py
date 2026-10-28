import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

TABLES = {"groups", "members", "expenses", "expense_splits", "settlements", "reversals"}
VIEWS = {"active_expenses", "active_settlements"}


async def add_room(connection, slug="room0001"):
    return await connection.scalar(
        text("""
            INSERT INTO groups (slug, kind, name, expires_at)
            VALUES (:slug, 'DEMO', 'Schema test', now() + interval '1 day') RETURNING id
        """),
        {"slug": slug},
    )


async def add_member(connection, group_id, name="Asha"):
    return await connection.scalar(
        text("""
            INSERT INTO members (group_id, version, name, color)
            VALUES (:gid, 1, :name, '#17BEBB') RETURNING id
        """),
        {"gid": group_id, "name": name},
    )


async def add_expense(connection, group_id, payer, *, lineage_id=None, revision_of=None):
    expense_id = await connection.scalar(text("SELECT nextval('expenses_id_seq')"))
    return await connection.scalar(
        text("""
            INSERT INTO expenses (id, group_id, version, lineage_id, revision_of, paid_by,
                                  title, spent_on, total_paise, split_type, idempotency_key,
                                  payload_hash, response_json)
            VALUES (:id, :gid, 2, :lineage, :revision, :payer, 'Lunch', CURRENT_DATE, 100,
                    'EQUAL', :key, :hash, '{}') RETURNING id
        """),
        {
            "id": expense_id,
            "gid": group_id,
            "lineage": expense_id if lineage_id is None else lineage_id,
            "revision": revision_of,
            "payer": payer,
            "key": f"expense-{expense_id:016d}",
            "hash": "a" * 64,
        },
    )


async def add_split(connection, group_id, expense_id, member_id, amount=100):
    await connection.execute(
        text("""
            INSERT INTO expense_splits (group_id, expense_id, member_id, share_paise)
            VALUES (:gid, :expense, :member, :amount)
        """),
        {"gid": group_id, "expense": expense_id, "member": member_id, "amount": amount},
    )


async def add_settlement(
    connection, group_id, sender, receiver, *, mode="PROTECTED", key="settlement-key-0001"
):
    return await connection.scalar(
        text("""
            INSERT INTO settlements (group_id, version, from_member, to_member, amount_paise,
                                     mode, idempotency_key, payload_hash, graph_version_before,
                                     response_json)
            VALUES (:gid, 3, :sender, :receiver, 100, :mode, :key, :hash, 2, '{}') RETURNING id
        """),
        {
            "gid": group_id,
            "sender": sender,
            "receiver": receiver,
            "mode": mode,
            "key": key,
            "hash": "a" * 64,
        },
    )


async def add_reversal(
    connection,
    group_id,
    *,
    expense_id=None,
    settlement_id=None,
    reason="DELETE",
    key="delete-key-00001",
):
    return await connection.scalar(
        text("""
            INSERT INTO reversals (group_id, version, expense_id, settlement_id, reason,
                                   idempotency_key, payload_hash, response_json)
            VALUES (:gid, 4, :expense, :settlement, :reason, :key, :hash, CAST(:response AS jsonb))
            RETURNING id
        """),
        {
            "gid": group_id,
            "expense": expense_id,
            "settlement": settlement_id,
            "reason": reason,
            "key": key,
            "hash": None if key is None else "a" * 64,
            "response": None if key is None else "{}",
        },
    )


@pytest.fixture
async def room(engine):
    async with engine.begin() as connection:
        group_id = await add_room(connection)
        first = await add_member(connection, group_id)
        second = await add_member(connection, group_id, "Bina")
    return group_id, first, second


async def test_all_schema_objects_exist(engine):
    async with engine.connect() as connection:
        tables = set(
            await connection.scalars(
                text("""
            SELECT tablename FROM pg_tables WHERE schemaname = 'public'
        """)
            )
        )
        views = set(
            await connection.scalars(
                text("""
            SELECT viewname FROM pg_views WHERE schemaname = 'public'
        """)
            )
        )
        assert tables == TABLES | {"alembic_version"}
        assert views == VIEWS
        indexes = dict(
            (
                await connection.execute(
                    text("""
            SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
        """)
                )
            ).all()
        )
        assert {
            "members_group_name_ci",
            "settlements_idem_protected",
            "expenses_group_version",
            "expenses_group_spent",
            "expenses_group_lineage",
            "expense_splits_group",
            "settlements_group_version",
            "reversals_group_version",
            "groups_kind_expires",
        } <= indexes.keys()
        assert "WHERE (mode = 'PROTECTED'::text)" in indexes["settlements_idem_protected"]
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0001_initial"
        )


async def test_upgrade_downgrade_upgrade(engine, migrated_database):
    _, migrate = migrated_database
    await asyncio.to_thread(migrate, "downgrade", "base")
    try:
        async with engine.connect() as connection:
            for name in sorted(TABLES | VIEWS):
                assert (
                    await connection.scalar(
                        text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
                    )
                    is None
                )
    finally:
        await asyncio.to_thread(migrate, "upgrade", "head")
    await test_all_schema_objects_exist(engine)


async def test_slug_check_rejects_short_names(engine):
    with pytest.raises(IntegrityError) as error:
        async with engine.begin() as connection:
            await add_room(connection, "short")
    assert error.value.orig.sqlstate == "23514"


async def test_member_names_are_unique_per_room_ignoring_case(engine):
    async with engine.begin() as connection:
        first_room = await add_room(connection, "room0001")
        second_room = await add_room(connection, "room0002")
        await add_member(connection, first_room, "Asha")
        await add_member(connection, second_room, "asha")
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_member(connection, first_room, "asha")
        assert error.value.orig.sqlstate == "23505"


@pytest.mark.parametrize("foreign_reference", ["payer", "split_member"])
async def test_cross_room_foreign_keys_are_rejected_at_commit(engine, room, foreign_reference):
    group_id, payer, _ = room
    async with engine.begin() as connection:
        other_room = await add_room(connection, "room0002")
        other_member = await add_member(connection, other_room)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        expense_id = await add_expense(
            connection, group_id, other_member if foreign_reference == "payer" else payer
        )
        assert expense_id > 0  # The INSERT succeeds; the deferred check must fail at COMMIT.
        if foreign_reference == "split_member":
            await add_split(connection, group_id, expense_id, other_member)
        with pytest.raises(IntegrityError) as error:
            await transaction.commit()
        assert error.value.orig.sqlstate == "23503"
        await connection.rollback()


async def test_original_expense_requires_its_own_lineage_id(engine, room):
    group_id, payer, _ = room
    async with engine.begin() as connection:
        original = await add_expense(connection, group_id, payer)
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_expense(connection, group_id, payer, lineage_id=original)
        assert error.value.orig.sqlstate == "23514"


async def test_expense_can_have_only_one_successor(engine, room):
    group_id, payer, _ = room
    async with engine.begin() as connection:
        original = await add_expense(connection, group_id, payer)
        await add_expense(connection, group_id, payer, lineage_id=original, revision_of=original)
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_expense(
                    connection, group_id, payer, lineage_id=original, revision_of=original
                )
        assert error.value.orig.sqlstate == "23505"


async def test_expense_can_be_reversed_only_once(engine, room):
    group_id, payer, _ = room
    async with engine.begin() as connection:
        expense_id = await add_expense(connection, group_id, payer)
        await add_reversal(connection, group_id, expense_id=expense_id)
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_reversal(
                    connection, group_id, expense_id=expense_id, key="delete-key-00002"
                )
        assert error.value.orig.sqlstate == "23505"


async def test_edit_reversal_cannot_have_an_idempotency_key(engine, room):
    group_id, payer, _ = room
    async with engine.begin() as connection:
        expense_id = await add_expense(connection, group_id, payer)
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_reversal(connection, group_id, expense_id=expense_id, reason="EDIT")
        assert error.value.orig.sqlstate == "23514"


async def test_delete_reversal_of_settlement_is_valid(engine, room):
    group_id, sender, receiver = room
    async with engine.begin() as connection:
        settlement = await add_settlement(connection, group_id, sender, receiver)
        reversal = await add_reversal(connection, group_id, settlement_id=settlement)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT settlement_id FROM reversals WHERE id=:id"), {"id": reversal}
            )
            == settlement
        )


async def test_protected_settlement_key_is_unique(engine, room):
    group_id, sender, receiver = room
    async with engine.begin() as connection:
        await add_settlement(connection, group_id, sender, receiver)
        with pytest.raises(IntegrityError) as error:
            async with connection.begin_nested():
                await add_settlement(connection, group_id, sender, receiver)
        assert error.value.orig.sqlstate == "23505"


async def test_naive_settlement_key_can_repeat(engine, room):
    group_id, sender, receiver = room
    async with engine.begin() as connection:
        first = await add_settlement(connection, group_id, sender, receiver, mode="NAIVE")
        second = await add_settlement(connection, group_id, sender, receiver, mode="NAIVE")
        assert first != second
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 2


async def test_views_hide_reversed_rows(engine, room):
    group_id, sender, receiver = room
    async with engine.begin() as connection:
        reversed_expense = await add_expense(connection, group_id, sender)
        active_expense = await add_expense(connection, group_id, sender)
        reversed_settlement = await add_settlement(connection, group_id, sender, receiver)
        active_settlement = await add_settlement(
            connection, group_id, sender, receiver, key="settlement-key-0002"
        )
        await add_reversal(connection, group_id, expense_id=reversed_expense)
        await add_reversal(
            connection, group_id, settlement_id=reversed_settlement, key="delete-key-00002"
        )
    async with engine.connect() as connection:
        assert list(await connection.scalars(text("SELECT id FROM active_expenses"))) == [
            active_expense
        ]
        assert list(await connection.scalars(text("SELECT id FROM active_settlements"))) == [
            active_settlement
        ]
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 2
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 2


async def test_room_delete_cascades_through_edit_and_settlement_history(engine, room):
    group_id, sender, receiver = room
    async with engine.begin() as connection:
        original = await add_expense(connection, group_id, sender)
        await add_split(connection, group_id, original, sender, 50)
        await add_split(connection, group_id, original, receiver, 50)
        await add_reversal(connection, group_id, expense_id=original, reason="EDIT", key=None)
        edited = await add_expense(
            connection, group_id, sender, lineage_id=original, revision_of=original
        )
        await add_split(connection, group_id, edited, sender, 50)
        await add_split(connection, group_id, edited, receiver, 50)
        settlement = await add_settlement(connection, group_id, sender, receiver)
        await add_reversal(connection, group_id, settlement_id=settlement)
    async with engine.begin() as connection:
        for table in TABLES:
            column = "id" if table == "groups" else "group_id"
            assert (
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE {column}=:gid"), {"gid": group_id}
                )
                > 0
            )
        await connection.execute(text("DELETE FROM groups WHERE id=:gid"), {"gid": group_id})
    async with engine.connect() as connection:
        for table in TABLES:
            column = "id" if table == "groups" else "group_id"
            assert (
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE {column}=:gid"), {"gid": group_id}
                )
                == 0
            )
