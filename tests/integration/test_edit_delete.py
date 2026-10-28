import ast
import asyncio
import re
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

SEED_BALANCES = {
    "Aarav": 500000,
    "Priya": 350000,
    "Rohan": 250000,
    "Sneha": -600000,
    "Karthik": -500000,
}
EDITED_BALANCES = {
    "Aarav": 496000,
    "Priya": 366000,
    "Rohan": 246000,
    "Sneha": -604000,
    "Karthik": -504000,
}
DELETED_BALANCES = {
    "Aarav": 692000,
    "Priya": -418000,
    "Rohan": 442000,
    "Sneha": -408000,
    "Karthik": -308000,
}


def headers(key):
    return {"Idempotency-Key": f"phase4-{key}-0000"}


def rupees(paise):
    return f"{paise // 100}.{paise % 100:02d}"


async def expense_id(engine, slug, title):
    async with engine.connect() as connection:
        return await connection.scalar(
            text("""
                SELECT e.id FROM expenses e JOIN groups g ON g.id=e.group_id
                WHERE g.slug=:slug AND e.title=:title
            """),
            {"slug": slug, "title": title},
        )


async def split_rows(engine, target):
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("""
                    SELECT member_id, weight, share_paise FROM expense_splits
                    WHERE expense_id=:id ORDER BY member_id
                """),
                {"id": target},
            )
        ).all()


async def expense_body(engine, target):
    async with engine.connect() as connection:
        row = (
            (await connection.execute(text("SELECT * FROM expenses WHERE id=:id"), {"id": target}))
            .mappings()
            .one()
        )
    participants = []
    for member, weight, share in await split_rows(engine, target):
        participant = {"id": member}
        if row["split_type"] == "SHARES":
            participant["weight"] = weight
        elif row["split_type"] == "EXACT":
            participant["amount"] = rupees(share)
        participants.append(participant)
    return {
        "title": row["title"],
        "paid_by": row["paid_by"],
        "amount": rupees(row["total_paise"]),
        "spent_on": row["spent_on"].isoformat(),
        "split": {"type": row["split_type"], "members": participants},
    }


async def assert_state(engine, integrity, demo, balances, version):
    report = await integrity(demo["slug"])
    assert report["zero_sum_paise"] == 0, report
    assert all(drift == 0 for drift in report["drift"].values()), report
    assert report["bad_split_expenses"] == 0, report
    assert report["orphan_edits"] == 0, report
    assert {name: report["cached"][member] for name, member in demo["members"].items()} == balances
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == version
        )


async def test_ordered_edit_delete_sequence(
    client, engine, demo, integrity, record_testsuite_property
):
    slug = demo["slug"]
    url = f"/api/rooms/{slug}/expenses"
    original = await expense_id(engine, slug, "Fuel & tolls")
    cafe = await expense_id(engine, slug, "Cafe brunch")
    body = {**await expense_body(engine, original), "amount": "9800"}
    async with engine.connect() as connection:
        initial_expenses = (
            (await connection.execute(text("SELECT * FROM expenses ORDER BY id"))).mappings().all()
        )
        initial_splits = (
            (
                await connection.execute(
                    text("SELECT * FROM expense_splits ORDER BY expense_id, member_id")
                )
            )
            .mappings()
            .all()
        )
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)

    first = await client.put(f"{url}/{original}", json=body, headers=headers("first-edit"))
    assert first.status_code == 200
    edited = first.json()
    current, version = edited["expense_id"], 10
    assert edited == {
        "expense_id": current,
        "lineage_id": original,
        "replaces": original,
        "graph_version": version,
        "shares": [
            {"member_id": member, "share_paise": 196000}
            for member in sorted(demo["members"].values())
        ],
    }
    assert current != original
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    unchanged = await client.put(f"{url}/{current}", json=body, headers=headers("unchanged"))
    assert unchanged.status_code == 200
    assert unchanged.json() == {"unchanged": True, "expense_id": current}
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 9
        assert await connection.scalar(text("SELECT count(*) FROM reversals")) == 1
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    old = await client.put(f"{url}/{original}", json=body, headers=headers("old-version"))
    assert old.status_code == 409
    assert old.json()["code"] == "EXPENSE_CHANGED"
    assert old.json()["current_expense_id"] == current
    assert old.json()["deleted"] is False
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    raced = await asyncio.gather(
        *(
            client.put(
                f"{url}/{current}",
                json={**body, "title": f"Fuel & tolls #{i}"},
                headers=headers(f"distinct-edit-{i}"),
            )
            for i in range(10)
        )
    )
    assert Counter(response.status_code for response in raced) == {200: 1, 409: 9}
    winner_index = next(i for i, response in enumerate(raced) if response.status_code == 200)
    winner = raced[winner_index].json()
    assert winner["replaces"] == current
    current, version = winner["expense_id"], version + 1
    assert winner["lineage_id"] == original
    assert winner["graph_version"] == version
    for response in raced:
        if response.status_code == 409:
            assert response.json()["code"] == "EXPENSE_CHANGED"
            assert response.json()["current_expense_id"] == current
            assert response.json()["deleted"] is False
    body = {**body, "title": "Fuel & tolls shared edit"}
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    repeated = await asyncio.gather(
        *(
            client.put(f"{url}/{current}", json=body, headers=headers("same-edit"))
            for _ in range(10)
        )
    )
    assert [response.status_code for response in repeated] == [200] * 10
    assert Counter(response.headers.get("Idempotent-Replayed") for response in repeated) == {
        None: 1,
        "true": 9,
    }
    shared = repeated[0].json()
    assert all(response.json() == shared for response in repeated)
    assert shared["replaces"] == current
    current, version = shared["expense_id"], version + 1
    assert shared["lineage_id"] == original
    assert shared["graph_version"] == version
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 11
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    mixed_requests = [
        (
            operation,
            i,
            client.delete(f"{url}/{current}", headers=headers(f"mixed-delete-{i}"))
            if operation == "DELETE"
            else client.put(
                f"{url}/{current}",
                json={**body, "title": f"Fuel & tolls mixed #{i}"},
                headers=headers(f"mixed-edit-{i}"),
            ),
        )
        for i in range(5)
        for operation in ("DELETE", "EDIT")
    ]
    mixed = await asyncio.gather(*(request for _, _, request in mixed_requests))
    assert Counter(response.status_code for response in mixed) == {200: 1, 409: 9}
    mixed_index = next(i for i, response in enumerate(mixed) if response.status_code == 200)
    operation, title_index, _ = mixed_requests[mixed_index]
    record_testsuite_property("mixed_race_winner", operation)
    version += 1
    successful = mixed[mixed_index].json()
    assert successful["graph_version"] == version
    live_id = None if operation == "DELETE" else successful["expense_id"]
    for response in mixed:
        if response.status_code == 409:
            assert response.json()["code"] == "EXPENSE_CHANGED"
            assert response.json()["current_expense_id"] == live_id
            assert response.json()["deleted"] is (operation == "DELETE")
    if operation == "DELETE":
        assert successful["deleted_expense_id"] == current
        await assert_state(engine, integrity, demo, DELETED_BALANCES, version)
        # DECISION: Restore a delete winner so later rows still exercise a live ₹9,800 expense.
        restored = await client.post(f"{url}/{current}/restore", headers=headers("mixed-restore"))
        assert restored.status_code == 201
        version += 1
        assert restored.json() == {
            "expense_id": restored.json()["expense_id"],
            "lineage_id": original,
            "restores": current,
            "graph_version": version,
        }
        current = restored.json()["expense_id"]
    else:
        assert successful["replaces"] == current
        current = live_id
        body = {**body, "title": f"Fuel & tolls mixed #{title_index}"}
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    deleted_id = current
    delete_headers = headers("delete-fuel")
    deleted = await client.delete(f"{url}/{deleted_id}", headers=delete_headers)
    assert deleted.status_code == 200
    version += 1
    assert deleted.json() == {
        "deleted_expense_id": deleted_id,
        "lineage_id": original,
        "graph_version": version,
    }
    await assert_state(engine, integrity, demo, DELETED_BALANCES, version)

    replay = await client.delete(f"{url}/{deleted_id}", headers=delete_headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == deleted.json()
    await assert_state(engine, integrity, demo, DELETED_BALANCES, version)
    again = await client.delete(f"{url}/{deleted_id}", headers=headers("delete-fuel-again"))
    assert again.status_code == 409
    assert again.json()["code"] == "EXPENSE_CHANGED"
    assert again.json()["deleted"] is True
    assert again.json()["current_expense_id"] is None
    await assert_state(engine, integrity, demo, DELETED_BALANCES, version)

    restored = await client.post(f"{url}/{deleted_id}/restore", headers=headers("restore-fuel"))
    assert restored.status_code == 201
    version += 1
    current = restored.json()["expense_id"]
    assert restored.json() == {
        "expense_id": current,
        "lineage_id": original,
        "restores": deleted_id,
        "graph_version": version,
    }
    assert current != deleted_id
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    cafe_before = await expense_body(engine, cafe)
    cafe_splits = await split_rows(engine, cafe)
    assert cafe_splits == [
        (demo["members"]["Priya"], 1, 60000),
        (demo["members"]["Rohan"], 1, 60000),
        (demo["members"]["Karthik"], 2, 120000),
    ]
    removed_cafe = await client.delete(f"{url}/{cafe}", headers=headers("delete-cafe"))
    assert removed_cafe.status_code == 200
    version += 1
    assert removed_cafe.json()["graph_version"] == version
    without_cafe = {**EDITED_BALANCES, "Priya": 186000, "Rohan": 306000, "Karthik": -384000}
    await assert_state(engine, integrity, demo, without_cafe, version)
    restored_cafe = await client.post(f"{url}/{cafe}/restore", headers=headers("restore-cafe"))
    assert restored_cafe.status_code == 201
    version += 1
    cafe_copy = restored_cafe.json()["expense_id"]
    assert restored_cafe.json() == {
        "expense_id": cafe_copy,
        "lineage_id": cafe,
        "restores": cafe,
        "graph_version": version,
    }
    assert await split_rows(engine, cafe_copy) == cafe_splits
    assert await expense_body(engine, cafe_copy) == cafe_before
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    payment = await client.post(
        f"/api/rooms/{slug}/settlements",
        json={
            "from_member": demo["members"]["Sneha"],
            "to_member": demo["members"]["Aarav"],
            "amount": "4960",
            "expected_graph_version": version,
        },
        headers=headers("record-plan"),
    )
    assert payment.status_code == 201
    version += 1
    settlement_id = payment.json()["settlement_id"]
    assert payment.json()["graph_version_after"] == version
    paid = {**EDITED_BALANCES, "Aarav": 0, "Sneha": -108000}
    await assert_state(engine, integrity, demo, paid, version)
    deleted_settlements = await asyncio.gather(
        *(
            client.delete(
                f"/api/rooms/{slug}/settlements/{settlement_id}",
                headers=headers(f"delete-settlement-{i}"),
            )
            for i in range(10)
        )
    )
    assert Counter(response.status_code for response in deleted_settlements) == {200: 1, 409: 9}
    version += 1
    for response in deleted_settlements:
        if response.status_code == 200:
            assert response.json() == {
                "deleted_settlement_id": settlement_id,
                "graph_version": version,
            }
        else:
            assert response.json()["code"] == "SETTLEMENT_ALREADY_DELETED"
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)

    reused = await client.put(
        f"{url}/{current}", json={**body, "title": "Reused key"}, headers=delete_headers
    )
    assert reused.status_code == 422
    assert reused.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    await assert_state(engine, integrity, demo, EDITED_BALANCES, version)
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT * FROM expenses WHERE id=ANY(:ids) ORDER BY id"),
                {"ids": [row["id"] for row in initial_expenses]},
            )
        ).mappings().all() == initial_expenses
        assert (
            await connection.execute(
                text("""
                    SELECT * FROM expense_splits WHERE expense_id=ANY(:ids)
                    ORDER BY expense_id, member_id
                """),
                {"ids": [row["id"] for row in initial_expenses]},
            )
        ).mappings().all() == initial_splits
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 14
        assert await connection.scalar(text("SELECT count(*) FROM reversals")) == 7


@pytest.mark.parametrize("operation", ["edit", "delete", "restore"])
async def test_other_rooms_expense_ids_are_not_visible(client, engine, demo, integrity, operation):
    other = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert other.status_code == 201
    foreign = await expense_id(engine, other.json()["slug"], "Fuel & tolls")
    own = await expense_id(engine, demo["slug"], "Fuel & tolls")
    url = f"/api/rooms/{demo['slug']}/expenses/{foreign}"
    if operation == "edit":
        response = await client.put(
            url, json=await expense_body(engine, own), headers=headers("foreign-edit")
        )
    elif operation == "delete":
        response = await client.delete(url, headers=headers("foreign-delete"))
    else:
        response = await client.post(f"{url}/restore", headers=headers("foreign-restore"))
    assert response.status_code == 404
    assert response.json()["code"] == "EXPENSE_NOT_FOUND"
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)
    report = await integrity(other.json()["slug"])
    assert report["zero_sum_paise"] == report["bad_split_expenses"] == report["orphan_edits"] == 0
    assert all(drift == 0 for drift in report["drift"].values())


async def test_live_expense_cannot_be_restored(client, engine, demo, integrity):
    target = await expense_id(engine, demo["slug"], "Fuel & tolls")
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses/{target}/restore", headers=headers("restore-live")
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EXPENSE_CHANGED"
    assert response.json()["current_expense_id"] == target
    assert response.json()["deleted"] is False
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)


async def test_edit_rejects_a_payer_from_another_room(client, engine, demo, integrity):
    other = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Other room", "members": ["Asha", "Bina"]}
    )
    assert other.status_code == 201
    async with engine.connect() as connection:
        foreign = await connection.scalar(
            text("SELECT m.id FROM members m JOIN groups g ON g.id=m.group_id WHERE g.slug=:slug"),
            {"slug": other.json()["slug"]},
        )
    target = await expense_id(engine, demo["slug"], "Fuel & tolls")
    body = {**await expense_body(engine, target), "paid_by": foreign}
    response = await client.put(
        f"/api/rooms/{demo['slug']}/expenses/{target}", json=body, headers=headers("foreign-payer")
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MEMBER_NOT_IN_GROUP"
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)


async def test_restore_copies_nondivisible_equal_shares(client, engine, demo, integrity):
    body = {
        "title": "Three-way remainder",
        "paid_by": demo["members"]["Rohan"],
        "amount": "100",
        "split": {
            "type": "EQUAL",
            "members": [{"id": demo["members"][name]} for name in ("Rohan", "Sneha", "Karthik")],
        },
    }
    url = f"/api/rooms/{demo['slug']}/expenses"
    added = await client.post(url, json=body, headers=headers("equal-add"))
    assert added.status_code == 201
    target = added.json()["expense_id"]
    original_splits = await split_rows(engine, target)
    expected = SEED_BALANCES.copy()
    expected["Rohan"] += 10000
    names = {member: name for name, member in demo["members"].items()}
    for member, _, share in original_splits:
        expected[names[member]] -= share
    await assert_state(engine, integrity, demo, expected, 10)
    deleted = await client.delete(f"{url}/{target}", headers=headers("equal-delete"))
    assert deleted.status_code == 200
    await assert_state(engine, integrity, demo, SEED_BALANCES, 11)
    restored = await client.post(f"{url}/{target}/restore", headers=headers("equal-restore"))
    assert restored.status_code == 201
    copy = restored.json()["expense_id"]
    assert copy % 3 != target % 3
    assert await split_rows(engine, copy) == original_splits
    await assert_state(engine, integrity, demo, expected, 12)


@pytest.mark.parametrize("title", ["Fuel & tolls", "Cafe brunch", "Club night"])
async def test_unchanged_ignores_participant_order_and_amount_format(
    client, engine, demo, integrity, title
):
    target = await expense_id(engine, demo["slug"], title)
    body = await expense_body(engine, target)
    body["split"]["members"].reverse()
    response = await client.put(
        f"/api/rooms/{demo['slug']}/expenses/{target}", json=body, headers=headers("same-values")
    )
    assert response.status_code == 200
    assert response.json() == {"unchanged": True, "expense_id": target}
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 8
        assert await connection.scalar(text("SELECT count(*) FROM reversals")) == 0


def test_service_sql_keeps_the_ledger_append_only():
    forbidden = re.compile(
        r"\bUPDATE\s+(?:expenses|settlements|reversals|expense_splits)\b|\bDELETE\s+FROM\b",
        re.IGNORECASE,
    )
    services = Path(__file__).resolve().parents[2] / "app" / "services"
    allow_delete = {("rooms.py", "reset"), ("rooms.py", "sweep")}
    for path in sorted(services.glob("*.py")):
        source = path.read_text()
        allowed = [
            (node.lineno, node.end_lineno)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef) and (path.name, node.name) in allow_delete
        ]
        for match in forbidden.finditer(source):
            line = source[: match.start()].count("\n") + 1
            assert match.group().upper().startswith("DELETE") and any(
                first <= line <= last for first, last in allowed
            ), (path.name, line)


@pytest.mark.parametrize("operation", ["edit", "delete", "restore", "delete-settlement"])
async def test_all_new_routes_require_idempotency_keys(client, engine, demo, operation):
    target = await expense_id(engine, demo["slug"], "Fuel & tolls")
    url = f"/api/rooms/{demo['slug']}/expenses/{target}"
    if operation == "edit":
        response = await client.put(url, json=await expense_body(engine, target))
    elif operation == "restore":
        response = await client.post(f"{url}/restore")
    elif operation == "delete-settlement":
        response = await client.delete(f"/api/rooms/{demo['slug']}/settlements/{target}")
    else:
        response = await client.delete(url)
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_MISSING"


async def test_noop_precedes_cap_and_deleting_does_not_free_an_expense_row(
    client, app, engine, demo, integrity
):
    target = await expense_id(engine, demo["slug"], "Fuel & tolls")
    body = await expense_body(engine, target)
    app.state.settings.DEMO_LIMITS = app.state.settings.DEMO_LIMITS._replace(expense_rows=8)
    url = f"/api/rooms/{demo['slug']}/expenses/{target}"
    noop_key = headers("cap-noop")
    noop = await client.put(url, json=body, headers=noop_key)
    assert noop.json() == {"unchanged": True, "expense_id": target}
    changed = await client.put(url, json={**body, "title": "Changed"}, headers=noop_key)
    assert changed.status_code == 422
    assert changed.json()["code"] == "ROOM_LIMIT"
    assert changed.json()["limit"] == "expenses"
    await assert_state(engine, integrity, demo, SEED_BALANCES, 9)
    deleted = await client.delete(url, headers=headers("cap-delete"))
    assert deleted.status_code == 200
    restore = await client.post(f"{url}/restore", headers=headers("cap-restore"))
    assert restore.status_code == 422
    assert restore.json()["code"] == "ROOM_LIMIT"
    assert restore.json()["limit"] == "expenses"
    report = await integrity(demo["slug"])
    assert report["zero_sum_paise"] == report["bad_split_expenses"] == report["orphan_edits"] == 0
    assert all(drift == 0 for drift in report["drift"].values())
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 8
        assert await connection.scalar(text("SELECT count(*) FROM reversals")) == 1
        assert await connection.scalar(text("SELECT graph_version FROM groups")) == 10


async def test_restore_replays_after_later_edit_and_preserves_attribution(
    client, engine, demo, integrity
):
    target = await expense_id(engine, demo["slug"], "Fuel & tolls")
    url = f"/api/rooms/{demo['slug']}/expenses"
    actor = demo["members"]["Priya"]
    removed = await client.delete(
        f"{url}/{target}",
        headers={**headers("attributed-delete"), "X-Acting-Member": str(actor)},
    )
    assert removed.status_code == 200
    restored = await client.post(
        f"{url}/{target}/restore",
        headers={**headers("attributed-restore"), "X-Acting-Member": str(actor)},
    )
    assert restored.status_code == 201
    copy = restored.json()["expense_id"]
    body = {**await expense_body(engine, copy), "title": "A later title"}
    edited = await client.put(
        f"{url}/{copy}",
        json=body,
        headers={**headers("attributed-edit"), "X-Acting-Member": str(actor)},
    )
    assert edited.status_code == 200
    replay = await client.post(
        f"{url}/{target}/restore",
        headers={**headers("attributed-restore"), "X-Acting-Member": str(demo["members"]["Sneha"])},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == restored.json()
    await assert_state(engine, integrity, demo, SEED_BALANCES, 12)
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT acted_by FROM expenses WHERE id=:id"), {"id": copy}
            )
            == actor
        )
        rows = (
            await connection.execute(
                text("""
            SELECT reason, acted_by, idempotency_key, payload_hash, response_json
            FROM reversals ORDER BY id
        """)
            )
        ).all()
        assert rows[0][:3] == ("DELETE", actor, headers("attributed-delete")["Idempotency-Key"])
        assert rows[1] == ("EDIT", actor, None, None, None)


async def test_naive_settlement_can_be_deleted_replayed_and_is_scoped_to_its_room(
    client, engine, demo, integrity
):
    slug = demo["slug"]
    payment = await client.post(
        f"/api/rooms/{slug}/settlements",
        json={
            "from_member": demo["members"]["Karthik"],
            "to_member": demo["members"]["Aarav"],
            "amount": "5000",
            "expected_graph_version": 9,
            "mode": "NAIVE",
        },
        headers=headers("single-naive"),
    )
    assert payment.status_code == 201
    sid = payment.json()["settlement_id"]
    other = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Other", "members": ["Asha", "Ravi"]}
    )
    assert other.status_code == 201
    foreign = await client.delete(
        f"/api/rooms/{other.json()['slug']}/settlements/{sid}", headers=headers("foreign-payment")
    )
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "SETTLEMENT_NOT_FOUND"
    url = f"/api/rooms/{slug}/settlements/{sid}"
    actor = demo["members"]["Priya"]
    deletion_headers = {**headers("naive-delete"), "X-Acting-Member": str(actor)}
    deleted = await client.delete(url, headers=deletion_headers)
    assert deleted.status_code == 200
    replay = await client.delete(
        url, headers={**deletion_headers, "X-Acting-Member": str(demo["members"]["Sneha"])}
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == deleted.json()
    await assert_state(engine, integrity, demo, SEED_BALANCES, 11)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM active_settlements")) == 0
        assert (
            await connection.scalar(
                text("SELECT acted_by FROM reversals WHERE settlement_id=:id"), {"id": sid}
            )
            == actor
        )
