import asyncio
from datetime import datetime

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_seed_snapshot_matches_every_verified_value(client, engine, demo):
    response = await client.get(f"/api/rooms/{demo['slug']}/state")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"v9"'
    snapshot = response.json()
    assert set(snapshot) == {
        "room",
        "members",
        "expenses",
        "settlements",
        "graph",
        "invariants",
        "limits",
        "usage",
    }
    assert snapshot["room"] == {
        "slug": demo["slug"],
        "kind": "DEMO",
        "name": "Goa Road Trip",
        "graph_version": 9,
        "expires_at": snapshot["room"]["expires_at"],
    }
    assert datetime.fromisoformat(snapshot["room"]["expires_at"]).tzinfo is not None
    names = ["Aarav", "Priya", "Rohan", "Sneha", "Karthik"]
    balances = [500000, 350000, 250000, -600000, -500000]
    colors = ["#E4572E", "#17BEBB", "#FFC914", "#76B041", "#7E52A0"]
    assert snapshot["members"] == [
        {"id": demo["members"][name], "name": name, "color": color, "net_balance_paise": balance}
        for name, color, balance in zip(names, colors, balances, strict=True)
    ]

    def edges(rows):
        return [
            {
                "from": demo["members"][sender],
                "to": demo["members"][receiver],
                "amount_paise": amount,
            }
            for sender, receiver, amount in rows
        ]

    graph = snapshot["graph"]
    assert graph["pairwise"] == {
        "count": 10,
        "truncated": False,
        "edges": edges(
            [
                ("Sneha", "Aarav", 288000),
                ("Karthik", "Aarav", 238000),
                ("Karthik", "Priya", 196000),
                ("Sneha", "Rohan", 136000),
                ("Sneha", "Priya", 126000),
                ("Karthik", "Rohan", 116000),
                ("Sneha", "Karthik", 50000),
                ("Rohan", "Priya", 40000),
                ("Aarav", "Rohan", 38000),
                ("Priya", "Aarav", 12000),
            ]
        ),
    }
    assert graph["nonzero_members"] == 5
    assert graph["recommended"] == "OPTIMAL"
    assert graph["greedy"]["count"] == 4
    assert graph["greedy"]["edges"] == edges(
        [
            ("Sneha", "Aarav", 500000),
            ("Karthik", "Priya", 350000),
            ("Karthik", "Rohan", 150000),
            ("Sneha", "Rohan", 100000),
        ]
    )
    assert graph["greedy"]["steps"] == [
        {
            "debtor": demo["members"][sender],
            "debt_paise": debt,
            "creditor": demo["members"][receiver],
            "credit_paise": credit,
            "amount_paise": amount,
        }
        for sender, receiver, debt, credit, amount in [
            ("Sneha", "Aarav", 600000, 500000, 500000),
            ("Karthik", "Priya", 500000, 350000, 350000),
            ("Karthik", "Rohan", 150000, 250000, 150000),
            ("Sneha", "Rohan", 100000, 100000, 100000),
        ]
    ]
    assert graph["optimal"] == {
        "count": 3,
        "edges": edges(
            [
                ("Sneha", "Priya", 350000),
                ("Sneha", "Rohan", 250000),
                ("Karthik", "Aarav", 500000),
            ]
        ),
        "groups": [
            [demo["members"][name] for name in group]
            for group in (("Sneha", "Rohan", "Priya"), ("Karthik", "Aarav"))
        ],
    }
    assert graph["chaos_target"] == edges([("Karthik", "Aarav", 500000)])[0]
    assert snapshot["invariants"] == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 0,
        "drifted_members": [],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }
    assert snapshot["limits"] == {"members": 12, "expense_rows": 200, "settlements": 500}
    assert snapshot["usage"] == {"members": 5, "expense_rows": 8, "settlements": 0}
    assert snapshot["settlements"] == []
    assert len(snapshot["expenses"]) == 8
    assert [row["id"] for row in snapshot["expenses"]] == sorted(
        (row["id"] for row in snapshot["expenses"]), reverse=True
    )
    totals = {
        "Villa booking (2 nights)": 1020000,
        "Fuel & tolls": 960000,
        "Beach-shack seafood dinner": 610000,
        "Scooter rentals": 330000,
        "Dudhsagar jeep safari": 580000,
        "Parasailing": 450000,
        "Cafe brunch": 240000,
        "Club night": 500000,
    }
    async with engine.connect() as connection:
        dates = dict((await connection.execute(text("SELECT title, spent_on FROM expenses"))).all())
    for expense in snapshot["expenses"]:
        assert set(expense) == {
            "id",
            "lineage_id",
            "title",
            "paid_by",
            "total_paise",
            "split_type",
            "spent_on",
            "revised",
            "acted_by",
            "shares",
        }
        assert expense["id"] == expense["lineage_id"]
        assert expense["revised"] is False and expense["acted_by"] is None
        assert expense["total_paise"] == totals[expense["title"]]
        assert expense["spent_on"] == dates[expense["title"]].isoformat()
        assert sum(share["share_paise"] for share in expense["shares"]) == expense["total_paise"]
        assert all(type(share["share_paise"]) is int for share in expense["shares"])


async def test_fuel_edit_changes_the_optimal_graph_count(client, engine, demo):
    async with engine.connect() as connection:
        target, spent_on = (
            await connection.execute(
                text("SELECT id, spent_on FROM expenses WHERE title='Fuel & tolls'")
            )
        ).one()
    edited = await client.put(
        f"/api/rooms/{demo['slug']}/expenses/{target}",
        headers={"Idempotency-Key": "snapshot-fuel-edit"},
        json={
            "title": "Fuel & tolls",
            "paid_by": demo["members"]["Priya"],
            "amount": "9800",
            "spent_on": spent_on.isoformat(),
            "split": {
                "type": "EQUAL",
                "members": [{"id": member} for member in demo["members"].values()],
            },
        },
    )
    assert edited.status_code == 200
    response = await client.get(f"/api/rooms/{demo['slug']}/state")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["room"]["graph_version"] == 10
    assert snapshot["graph"]["pairwise"]["count"] == 10
    assert snapshot["graph"]["greedy"]["count"] == 4
    assert snapshot["graph"]["optimal"]["count"] == 4
    assert snapshot["usage"]["expense_rows"] == 9
    rows = {expense["id"]: expense for expense in snapshot["expenses"]}
    assert target not in rows
    assert rows[edited.json()["expense_id"]]["revised"] is True


@pytest.mark.naive_drift
async def test_snapshot_reports_exact_naive_drift(client, demo):
    body = {
        "from_member": demo["members"]["Karthik"],
        "to_member": demo["members"]["Aarav"],
        "amount": "5000",
        "expected_graph_version": 9,
        "mode": "NAIVE",
    }
    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/rooms/{demo['slug']}/settlements",
                json=body,
                headers={"Idempotency-Key": f"snapshot-naive-{i:02d}"},
            )
            for i in range(10)
        )
    )
    assert [response.status_code for response in responses] == [201] * 10
    snapshot = (await client.get(f"/api/rooms/{demo['slug']}/state")).json()
    assert snapshot["invariants"] == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 9000000,
        "drifted_members": [
            {
                "member_id": demo["members"]["Aarav"],
                "cached_paise": 0,
                "ledger_paise": -4500000,
                "drift_paise": 4500000,
            },
            {
                "member_id": demo["members"]["Karthik"],
                "cached_paise": 0,
                "ledger_paise": 4500000,
                "drift_paise": -4500000,
            },
        ],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }
    assert snapshot["graph"]["pairwise"]["count"] == 10
    assert len(snapshot["settlements"]) == snapshot["usage"]["settlements"] == 10


async def test_large_group_uses_greedy_without_demo_tools(client, engine):
    created = await client.post(
        "/api/rooms",
        json={
            "kind": "GROUP",
            "name": "Large group",
            "members": [f"Member {i:02d}" for i in range(16)],
        },
    )
    assert created.status_code == 201
    slug = created.json()["slug"]
    async with engine.connect() as connection:
        ids = list(
            await connection.scalars(
                text("""
            SELECT m.id FROM members m JOIN groups g ON g.id=m.group_id
            WHERE g.slug=:slug ORDER BY m.id
        """),
                {"slug": slug},
            )
        )
    added = await client.post(
        f"/api/rooms/{slug}/expenses",
        headers={"Idempotency-Key": "large-group-expense"},
        json={
            "title": "Lunch",
            "paid_by": ids[0],
            "amount": "16",
            "split": {"type": "EQUAL", "members": [{"id": member} for member in ids]},
        },
    )
    assert added.status_code == 201
    response = await client.get(f"/api/rooms/{slug}/state")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["graph"]["nonzero_members"] == 16
    assert snapshot["graph"]["optimal"] is None
    assert snapshot["graph"]["recommended"] == "GREEDY"
    assert snapshot["graph"]["greedy"]["count"] == 15
    assert "chaos_target" not in snapshot["graph"]


async def test_etag_and_cache_follow_committed_versions(client, engine, demo):
    url = f"/api/rooms/{demo['slug']}/state"
    initial = await client.get(url)
    assert initial.status_code == 200 and initial.headers["ETag"] == '"v9"'
    cached = await client.get(url, headers={"If-None-Match": initial.headers["ETag"]})
    assert cached.status_code == 304 and cached.content == b""
    added = await client.post(f"/api/rooms/{demo['slug']}/members", json={"name": "Asha"})
    assert added.status_code == 201
    updated = await client.get(url, headers={"If-None-Match": initial.headers["ETag"]})
    assert updated.status_code == 200 and updated.headers["ETag"] == '"v10"'
    assert updated.json()["room"]["graph_version"] == 10
    assert [member["name"] for member in updated.json()["members"]][-1] == "Asha"
    unchanged = await client.get(url, headers={"If-None-Match": updated.headers["ETag"]})
    assert unchanged.status_code == 304 and unchanged.content == b""
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT graph_version FROM groups")) == 10


async def test_current_etag_is_304_without_building_a_snapshot(client, engine, demo):
    from app.services.state import _caches

    url = f"/api/rooms/{demo['slug']}/state"
    etag = (await client.get(url)).headers["ETag"]
    _caches.clear()  # another worker, or an evicted entry: nothing cached for this version
    cached = await client.get(url, headers={"If-None-Match": etag})
    assert cached.status_code == 304 and cached.headers["ETag"] == etag
    assert not any(_caches.values()), "a 304 must answer from the room row alone"
    stale = await client.get(url, headers={"If-None-Match": '"v8"'})
    assert stale.status_code == 200 and stale.headers["ETag"] == etag


@pytest.mark.parametrize("expired", [False, True])
async def test_missing_or_expired_snapshot_is_404(client, engine, demo, expired):
    slug = demo["slug"] if expired else "unknown00"
    if expired:
        assert (await client.get(f"/api/rooms/{slug}/state")).status_code == 200
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE groups SET expires_at=now()-interval '1 second' WHERE slug=:slug"),
                {"slug": slug},
            )
    response = await client.get(f"/api/rooms/{slug}/state")
    assert response.status_code == 404 and response.json()["code"] == "ROOM_NOT_FOUND"


async def test_snapshot_keeps_only_the_newest_fifty_active_expenses(client, engine, demo):
    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/rooms/{demo['slug']}/expenses",
                json={
                    "title": f"Extra receipt {i}",
                    "paid_by": demo["members"]["Aarav"],
                    "amount": "5",
                    "split": {
                        "type": "EQUAL",
                        "members": [{"id": member} for member in demo["members"].values()],
                    },
                },
                headers={"Idempotency-Key": f"snapshot-limit-{i:02d}"},
            )
            for i in range(43)
        )
    )
    assert [response.status_code for response in responses] == [201] * 43
    response = await client.get(f"/api/rooms/{demo['slug']}/state")
    assert response.status_code == 200
    snapshot = response.json()
    assert len(snapshot["expenses"]) == 50
    assert snapshot["usage"]["expense_rows"] == 51
    async with engine.connect() as connection:
        expected = list(
            await connection.scalars(
                text("SELECT id FROM active_expenses ORDER BY version DESC, id DESC LIMIT 50")
            )
        )
    assert [row["id"] for row in snapshot["expenses"]] == expected
