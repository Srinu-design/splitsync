import asyncio
from collections import Counter

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def settlement(
    demo, sender="Karthik", receiver="Aarav", amount="5000", version=9, mode="PROTECTED"
):
    return {
        "from_member": demo["members"][sender],
        "to_member": demo["members"][receiver],
        "amount": amount,
        "expected_graph_version": version,
        "mode": mode,
    }


async def test_protected_races_replay_and_changed_key(client, engine, demo, integrity):
    url = f"/api/rooms/{demo['slug']}/settlements"
    responses = await asyncio.gather(
        *(
            client.post(
                url, json=settlement(demo), headers={"Idempotency-Key": f"settlement-race-{i:02d}"}
            )
            for i in range(10)
        )
    )
    assert Counter(response.status_code for response in responses) == {201: 1, 409: 9}
    created = next(response.json() for response in responses if response.status_code == 201)
    assert created == {
        "settlement_id": created["settlement_id"],
        "from_member": demo["members"]["Karthik"],
        "to_member": demo["members"]["Aarav"],
        "amount_paise": 500000,
        "graph_version_before": 9,
        "graph_version_after": 10,
    }
    for response in responses:
        if response.status_code == 409:
            assert response.json()["code"] == "STALE_GRAPH_VERSION"
            assert response.json()["current_graph_version"] == 10
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 1
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 10
        )
    first_report = await integrity(demo["slug"])
    assert first_report["zero_sum_paise"] == 0
    assert all(drift == 0 for drift in first_report["drift"].values())

    body = settlement(demo, "Sneha", "Priya", "3500", version=10)
    headers = {"Idempotency-Key": "settlement-same-key"}
    repeated = await asyncio.gather(
        *(client.post(url, json=body, headers=headers) for _ in range(10))
    )
    assert Counter(response.status_code for response in repeated) == {201: 1, 200: 9}
    receipt = next(response.json() for response in repeated if response.status_code == 201)
    assert all(response.json() == receipt for response in repeated)
    assert receipt["graph_version_before"] == 10
    assert receipt["graph_version_after"] == 11
    assert receipt["amount_paise"] == 350000
    for response in repeated:
        if response.status_code == 200:
            assert response.headers["Idempotent-Replayed"] == "true"
    refreshed = await client.post(url, json={**body, "expected_graph_version": 11}, headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json() == receipt
    changed = await client.post(
        url, json={**body, "amount": "1000", "expected_graph_version": 11}, headers=headers
    )
    assert changed.status_code == 422
    assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 2
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 11
        )


@pytest.mark.naive_drift
async def test_naive_race_reproduces_exact_drift(client, engine, demo, integrity):
    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/rooms/{demo['slug']}/settlements",
                json=settlement(demo, mode="NAIVE"),
                headers={"Idempotency-Key": f"naive-settlement-{i:02d}"},
            )
            for i in range(10)
        )
    )
    assert [response.status_code for response in responses] == [201] * 10
    assert len({response.json()["settlement_id"] for response in responses}) == 10
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM settlements")) == 10
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 19
        )
        assert list(await connection.scalars(text("SELECT DISTINCT version FROM settlements"))) == [
            10
        ]
    report = await integrity(demo["slug"])
    aarav, karthik = demo["members"]["Aarav"], demo["members"]["Karthik"]
    assert report["zero_sum_paise"] == 0
    assert report["cached"][aarav] == report["cached"][karthik] == 0
    assert report["ledger"][aarav] == -4500000
    assert report["ledger"][karthik] == 4500000
    assert report["drift"][aarav] == 4500000
    assert report["drift"][karthik] == -4500000
    assert sum(abs(drift) for drift in report["drift"].values()) == 9000000


async def test_over_settlement_reports_maximum(client, demo):
    response = await client.post(
        f"/api/rooms/{demo['slug']}/settlements",
        json=settlement(demo, amount="5000.01"),
        headers={"Idempotency-Key": "settlement-over-key"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "OVER_SETTLEMENT"
    assert response.json()["max_paise"] == 500000


async def test_naive_is_disabled_in_groups(client, engine):
    response = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Flat", "members": ["Asha", "Bina"]}
    )
    assert response.status_code == 201
    slug = response.json()["slug"]
    async with engine.connect() as connection:
        members = dict(
            (
                await connection.execute(
                    text("""
            SELECT m.name, m.id FROM members m JOIN groups g ON g.id=m.group_id WHERE g.slug=:slug
        """),
                    {"slug": slug},
                )
            ).all()
        )
    response = await client.post(
        f"/api/rooms/{slug}/settlements",
        json={
            "from_member": members["Asha"],
            "to_member": members["Bina"],
            "amount": "1",
            "expected_graph_version": 1,
            "mode": "NAIVE",
        },
        headers={"Idempotency-Key": "settlement-group-key"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "NAIVE_MODE_DISABLED"


async def test_naive_can_be_disabled_for_demos(client, app, demo):
    app.state.settings.ENABLE_NAIVE_MODE = False
    response = await client.post(
        f"/api/rooms/{demo['slug']}/settlements",
        json=settlement(demo, mode="NAIVE"),
        headers={"Idempotency-Key": "settlement-disabled-key"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "NAIVE_MODE_DISABLED"


async def test_settlement_actor_is_stored_but_not_hashed(client, engine, demo):
    url = f"/api/rooms/{demo['slug']}/settlements"
    key = "settlement-actor-key"
    first = await client.post(
        url,
        json=settlement(demo),
        headers={"Idempotency-Key": key, "X-Acting-Member": str(demo["members"]["Priya"])},
    )
    assert first.status_code == 201
    replay = await client.post(
        url,
        json=settlement(demo, version=10),
        headers={"Idempotency-Key": key, "X-Acting-Member": str(demo["members"]["Rohan"])},
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT acted_by FROM settlements WHERE id=:id"),
                {"id": first.json()["settlement_id"]},
            )
            == demo["members"]["Priya"]
        )


async def test_settlement_requires_an_idempotency_key(client, demo):
    response = await client.post(f"/api/rooms/{demo['slug']}/settlements", json=settlement(demo))
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_MISSING"
