import asyncio

import pytest
from sqlalchemy import text

from tests.integration.test_edit_delete import expense_body, expense_id

pytestmark = pytest.mark.integration


async def test_reset_demo_room_after_edits_deletes_and_chaos(client, engine, demo):
    slug = demo["slug"]

    # 1. Edit an expense
    fuel_id = await expense_id(engine, slug, "Fuel & tolls")
    fuel_body = await expense_body(engine, fuel_id)
    edit_res = await client.put(
        f"/api/rooms/{slug}/expenses/{fuel_id}",
        json={**fuel_body, "amount": "9800"},
        headers={"Idempotency-Key": "reset-test-edit-0001"},
    )
    assert edit_res.status_code == 200
    new_fuel_id = edit_res.json()["expense_id"]

    # 2. Delete an expense
    del_res = await client.delete(
        f"/api/rooms/{slug}/expenses/{new_fuel_id}",
        headers={"Idempotency-Key": "reset-test-delete-0001"},
    )
    assert del_res.status_code == 200

    # 3. Restore it
    restore_res = await client.post(
        f"/api/rooms/{slug}/expenses/{new_fuel_id}/restore",
        headers={"Idempotency-Key": "reset-test-restore-0001"},
    )
    assert restore_res.status_code == 201
    current_version = restore_res.json()["graph_version"]

    # 4. Protected settlement
    prot_res = await client.post(
        f"/api/rooms/{slug}/settlements",
        json={
            "from_member": demo["members"]["Sneha"],
            "to_member": demo["members"]["Aarav"],
            "amount": "4960",
            "expected_graph_version": current_version,
            "mode": "PROTECTED",
        },
        headers={"Idempotency-Key": "reset-test-settle-0001"},
    )
    assert prot_res.status_code == 201
    current_version = prot_res.json()["graph_version_after"]

    # 5. Naive chaos run (10 concurrent naive settlements)
    chaos_responses = await asyncio.gather(
        *(
            client.post(
                f"/api/rooms/{slug}/settlements",
                json={
                    "from_member": demo["members"]["Karthik"],
                    "to_member": demo["members"]["Aarav"],
                    "amount": "5000",
                    "expected_graph_version": current_version,
                    "mode": "NAIVE",
                },
                headers={"Idempotency-Key": f"reset-test-chaos-{i:02d}"},
            )
            for i in range(10)
        )
    )
    assert [r.status_code for r in chaos_responses] == [201] * 10

    # Ensure drift was produced before reset
    snapshot_before = (await client.get(f"/api/rooms/{slug}/state")).json()
    assert snapshot_before["invariants"]["ledger_drift_total_paise"] > 0
    version_before = snapshot_before["room"]["graph_version"]

    # 6. POST reset
    # DECISION: Reset demo room restoring seed members and seed expenses with clean invariants.
    reset_res = await client.post(f"/api/rooms/{slug}/reset")
    assert reset_res.status_code == 200
    snapshot = reset_res.json()

    # Seed balances (Aarav 500000 · Priya 350000 · Rohan 250000 · Sneha -600000 · Karthik -500000)
    names_balances = {m["name"]: m["net_balance_paise"] for m in snapshot["members"]}
    assert names_balances == {
        "Aarav": 500000,
        "Priya": 350000,
        "Rohan": 250000,
        "Sneha": -600000,
        "Karthik": -500000,
    }

    # Invariants all 0 (drift gone)
    assert snapshot["invariants"] == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 0,
        "drifted_members": [],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }

    # graph_version = version before reset + 9
    assert snapshot["room"]["graph_version"] == version_before + 9

    # 8 expenses, 0 settlements
    assert len(snapshot["expenses"]) == 8
    assert len(snapshot["settlements"]) == 0
    assert snapshot["usage"]["expense_rows"] == 8
    assert snapshot["usage"]["settlements"] == 0


async def test_reset_errors_on_group_and_unknown_slug(client):
    # reset a GROUP -> 403 RESET_NOT_ALLOWED
    created = await client.post(
        "/api/rooms",
        json={"kind": "GROUP", "name": "Weekend Trip", "members": ["Asha", "Ravi"]},
    )
    assert created.status_code == 201
    group_slug = created.json()["slug"]

    res_group = await client.post(f"/api/rooms/{group_slug}/reset")
    assert res_group.status_code == 403
    assert res_group.json()["code"] == "RESET_NOT_ALLOWED"

    # unknown slug -> 404
    res_unknown = await client.post("/api/rooms/unknown00/reset")
    assert res_unknown.status_code == 404
    assert res_unknown.json()["code"] == "ROOM_NOT_FOUND"


async def test_delete_group_cascades_all_room_tables(client, engine, demo):
    slug = demo["slug"]

    # Produce history: edit, delete, restore, settlement
    fuel_id = await expense_id(engine, slug, "Fuel & tolls")
    fuel_body = await expense_body(engine, fuel_id)
    edit_res = await client.put(
        f"/api/rooms/{slug}/expenses/{fuel_id}",
        json={**fuel_body, "amount": "9800"},
        headers={"Idempotency-Key": "cascade-test-edit-0001"},
    )
    assert edit_res.status_code == 200
    new_fuel_id = edit_res.json()["expense_id"]

    del_res = await client.delete(
        f"/api/rooms/{slug}/expenses/{new_fuel_id}",
        headers={"Idempotency-Key": "cascade-test-delete-0001"},
    )
    assert del_res.status_code == 200

    restore_res = await client.post(
        f"/api/rooms/{slug}/expenses/{new_fuel_id}/restore",
        headers={"Idempotency-Key": "cascade-test-restore-0001"},
    )
    assert restore_res.status_code == 201
    current_version = restore_res.json()["graph_version"]

    settle_res = await client.post(
        f"/api/rooms/{slug}/settlements",
        json={
            "from_member": demo["members"]["Sneha"],
            "to_member": demo["members"]["Aarav"],
            "amount": "4960",
            "expected_graph_version": current_version,
            "mode": "PROTECTED",
        },
        headers={"Idempotency-Key": "cascade-test-settle-0001"},
    )
    assert settle_res.status_code == 201

    # Verify rows exist across all child tables
    async with engine.connect() as connection:
        gid = await connection.scalar(
            text("SELECT id FROM groups WHERE slug=:slug"), {"slug": slug}
        )
        for table in ("members", "expenses", "expense_splits", "settlements", "reversals"):
            count = await connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE group_id=:gid"), {"gid": gid}
            )
            assert count > 0, f"{table} should have rows before delete"

    # DELETE FROM groups on the room
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM groups WHERE id=:gid"), {"gid": gid})

    # Assert every table has 0 rows for that room
    # DECISION: Verify that foreign key cascades clear all six room tables completely.
    async with engine.connect() as connection:
        for table in (
            "groups",
            "members",
            "expenses",
            "expense_splits",
            "settlements",
            "reversals",
        ):
            column = "id" if table == "groups" else "group_id"
            count = await connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE {column}=:gid"), {"gid": gid}
            )
            assert count == 0, f"{table} should have 0 rows for room after cascade"
