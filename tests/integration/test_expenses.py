import asyncio

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def equal_expense(demo, title="Airport auto"):
    ids = demo["members"]
    return {
        "title": title,
        "paid_by": ids["Rohan"],
        "amount": "100",
        "split": {
            "type": "EQUAL",
            "members": [{"id": ids[name]} for name in ("Rohan", "Sneha", "Karthik")],
        },
    }


async def test_expense_replay_preserves_body_and_actor(client, engine, demo):
    url = f"/api/rooms/{demo['slug']}/expenses"
    body = equal_expense(demo)
    key = "expense-replay-key"
    first = await client.post(
        url,
        json=body,
        headers={"Idempotency-Key": key, "X-Acting-Member": str(demo["members"]["Priya"])},
    )
    assert first.status_code == 201
    replay = await client.post(
        url,
        json=body,
        headers={"Idempotency-Key": key, "X-Acting-Member": str(demo["members"]["Sneha"])},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert first.json()["lineage_id"] == first.json()["expense_id"]
    assert first.json()["graph_version"] == 10
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT acted_by FROM expenses WHERE id=:id"),
                {"id": first.json()["expense_id"]},
            )
            == demo["members"]["Priya"]
        )
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 9
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 10
        )


async def test_expense_key_cannot_be_reused_for_a_changed_body(client, demo):
    url = f"/api/rooms/{demo['slug']}/expenses"
    headers = {"Idempotency-Key": "expense-changed-key"}
    assert (await client.post(url, json=equal_expense(demo), headers=headers)).status_code == 201
    changed = await client.post(url, json=equal_expense(demo, "Different title"), headers=headers)
    assert changed.status_code == 422
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_idempotency_keys_cannot_cross_write_operations(client, demo):
    headers = {"Idempotency-Key": "shared-operation-key"}
    added = await client.post(
        f"/api/rooms/{demo['slug']}/expenses", json=equal_expense(demo), headers=headers
    )
    assert added.status_code == 201
    settlement = await client.post(
        f"/api/rooms/{demo['slug']}/settlements",
        headers=headers,
        json={
            "from_member": demo["members"]["Karthik"],
            "to_member": demo["members"]["Aarav"],
            "amount": "1",
            "expected_graph_version": 10,
        },
    )
    assert settlement.status_code == 422
    assert settlement.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_actor_id_must_fit_postgres_bigint(client, demo):
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=equal_expense(demo),
        headers={"Idempotency-Key": "expense-large-actor", "X-Acting-Member": str(10**100)},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("key", [None, "short", "contains spaces key", "x" * 65])
async def test_expense_requires_a_valid_key(client, demo, key):
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=equal_expense(demo),
        headers={} if key is None else {"Idempotency-Key": key},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_MISSING"


async def test_exact_mismatch_reports_both_totals(client, demo):
    body = equal_expense(demo)
    body["split"] = {
        "type": "EXACT",
        "members": [
            {"id": demo["members"]["Rohan"], "amount": "40"},
            {"id": demo["members"]["Sneha"], "amount": "50"},
        ],
    }
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=body,
        headers={"Idempotency-Key": "expense-mismatch-key"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "EXACT_SUM_MISMATCH"
    assert response.json()["expected_paise"] == 10000
    assert response.json()["got_paise"] == 9000


@pytest.mark.parametrize("foreign_field", ["payer", "participant", "actor"])
async def test_expense_rejects_foreign_members(client, engine, demo, foreign_field):
    other = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Other flat", "members": ["Asha", "Bina"]}
    )
    assert other.status_code == 201
    async with engine.connect() as connection:
        foreign_id = await connection.scalar(
            text("SELECT m.id FROM members m JOIN groups g ON g.id=m.group_id WHERE g.slug=:slug"),
            {"slug": other.json()["slug"]},
        )
    body = equal_expense(demo)
    headers = {"Idempotency-Key": "expense-foreign-key"}
    if foreign_field == "payer":
        body["paid_by"] = foreign_id
    elif foreign_field == "participant":
        body["split"]["members"][0]["id"] = foreign_id
    else:
        headers["X-Acting-Member"] = str(foreign_id)
    response = await client.post(f"/api/rooms/{demo['slug']}/expenses", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "MEMBER_NOT_IN_GROUP"


async def test_equal_split_rotates_the_extra_paisa(client, demo):
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=equal_expense(demo),
        headers={"Idempotency-Key": "expense-rotation-key"},
    )
    assert response.status_code == 201
    body = response.json()
    ids = sorted(demo["members"][name] for name in ("Rohan", "Sneha", "Karthik"))
    expected = dict.fromkeys(ids, 3333)
    expected[ids[body["expense_id"] % 3]] += 1
    shares = {share["member_id"]: share["share_paise"] for share in body["shares"]}
    assert shares == expected
    assert sum(shares.values()) == 10000


async def test_weighted_expense_uses_integer_shares(client, demo):
    ids = demo["members"]
    body = {
        "title": "Cafe brunch",
        "paid_by": ids["Priya"],
        "amount": "2400",
        "split": {
            "type": "SHARES",
            "members": [
                {"id": ids["Priya"], "weight": 1},
                {"id": ids["Rohan"], "weight": 1},
                {"id": ids["Karthik"], "weight": 2},
            ],
        },
    }
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=body,
        headers={"Idempotency-Key": "expense-weighted-key"},
    )
    assert response.status_code == 201
    assert {share["member_id"]: share["share_paise"] for share in response.json()["shares"]} == {
        ids["Priya"]: 60000,
        ids["Rohan"]: 60000,
        ids["Karthik"]: 120000,
    }


async def test_exact_expense_keeps_the_requested_shares(client, demo):
    body = equal_expense(demo)
    body["split"] = {
        "type": "EXACT",
        "members": [
            {"id": demo["members"]["Rohan"], "amount": "40"},
            {"id": demo["members"]["Sneha"], "amount": "60"},
        ],
    }
    response = await client.post(
        f"/api/rooms/{demo['slug']}/expenses",
        json=body,
        headers={"Idempotency-Key": "expense-exact-key"},
    )
    assert response.status_code == 201
    assert {share["member_id"]: share["share_paise"] for share in response.json()["shares"]} == {
        demo["members"]["Rohan"]: 4000,
        demo["members"]["Sneha"]: 6000,
    }


async def test_ten_different_expenses_all_commit(client, engine, demo, integrity):
    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/rooms/{demo['slug']}/expenses",
                json=equal_expense(demo, f"Auto {i}"),
                headers={"Idempotency-Key": f"concurrent-expense-{i:02d}"},
            )
            for i in range(10)
        )
    )
    assert [response.status_code for response in responses] == [201] * 10
    assert len({response.json()["expense_id"] for response in responses}) == 10
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 19
        )
        assert await connection.scalar(text("SELECT count(*) FROM expenses")) == 18
    report = await integrity(demo["slug"])
    assert report["zero_sum_paise"] == 0
    assert all(drift == 0 for drift in report["drift"].values())
