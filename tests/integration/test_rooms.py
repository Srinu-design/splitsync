import asyncio
from collections import Counter

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_demo_reproduces_seed(client, engine):
    response = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert response.status_code == 201
    room = response.json()
    assert room == {"slug": room["slug"], "url": f"/r/{room['slug']}", "kind": "DEMO"}
    async with engine.connect() as connection:
        group_id, version = (
            await connection.execute(
                text("SELECT id, graph_version FROM groups WHERE slug=:slug"),
                {"slug": room["slug"]},
            )
        ).one()
        assert version == 9
        balances = dict(
            (
                await connection.execute(
                    text("SELECT name, net_balance_paise FROM members WHERE group_id=:gid"),
                    {"gid": group_id},
                )
            ).all()
        )
        assert balances == {
            "Aarav": 500000,
            "Priya": 350000,
            "Rohan": 250000,
            "Sneha": -600000,
            "Karthik": -500000,
        }
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM expenses WHERE group_id=:gid"), {"gid": group_id}
            )
            == 8
        )


async def test_group_member_cap(client, engine):
    response = await client.post(
        "/api/rooms",
        json={"kind": "GROUP", "name": "Large flat", "members": [f"Member {i}" for i in range(50)]},
    )
    assert response.status_code == 201
    slug = response.json()["slug"]
    added = await client.post(f"/api/rooms/{slug}/members", json={"name": "Member 51"})
    assert added.status_code == 422
    assert added.json()["code"] == "ROOM_LIMIT"
    assert added.json()["limit"] == "members"
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("""
                SELECT count(*) FROM members m JOIN groups g ON g.id=m.group_id WHERE g.slug=:slug
            """),
                {"slug": slug},
            )
            == 50
        )


async def test_group_member_names_must_be_unique_ignoring_case(client):
    response = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Flat", "members": ["Asha", "asha"]}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_duplicate_member_name_rolls_back_version(client, engine, demo):
    response = await client.post(f"/api/rooms/{demo['slug']}/members", json={"name": "aarav"})
    assert response.status_code == 409
    assert response.json()["code"] == "MEMBER_NAME_TAKEN"
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 9
        )


async def test_unknown_room_returns_problem_details(client):
    response = await client.post("/api/rooms/unknown00/members", json={"name": "Asha"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["code"] == "ROOM_NOT_FOUND"
    assert problem["type"] == "/errors/room-not-found"
    assert problem["status"] == 404
    assert problem["title"] and problem["detail"]


async def test_demo_room_cap(client, app):
    app.state.settings.MAX_ACTIVE_DEMO_ROOMS = 2
    first = await client.post("/api/rooms", json={"kind": "DEMO"})
    second = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert first.status_code == second.status_code == 201
    third = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert third.status_code == 503
    assert third.json()["code"] == "ROOMS_FULL"
    assert third.headers["Retry-After"] == "600"


async def test_database_size_guard(client, app):
    app.state.settings.MAX_DB_SIZE_MB = 0
    response = await client.post("/api/rooms", json={"kind": "DEMO"})
    assert response.status_code == 503
    assert response.json()["code"] == "STORAGE_FULL"


async def test_add_member_bumps_version(client, engine, demo):
    response = await client.post(f"/api/rooms/{demo['slug']}/members", json={"name": "Asha"})
    assert response.status_code == 201
    assert response.json()["name"] == "Asha"
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT graph_version FROM groups WHERE slug=:slug"), {"slug": demo["slug"]}
            )
            == 10
        )


async def test_group_names_follow_postgres_case_folding(client, engine):
    response = await client.post(
        "/api/rooms", json={"kind": "GROUP", "name": "Unicode", "members": ["İ", "i"]}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM groups")) == 0


async def test_concurrent_creations_respect_the_room_cap(client, app):
    app.state.settings.MAX_ACTIVE_GROUPS = 2
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/rooms", json={"kind": "GROUP", "name": "Flat", "members": ["Asha", "Bina"]}
            )
            for _ in range(10)
        )
    )
    assert Counter(response.status_code for response in responses) == {201: 2, 503: 8}
    assert all(
        response.json()["code"] == "ROOMS_FULL"
        for response in responses
        if response.status_code == 503
    )


async def test_room_lock_timeout_is_a_retryable_busy_response(client, engine, demo):
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT id FROM groups WHERE slug=:slug FOR UPDATE"), {"slug": demo["slug"]}
        )
        response = await client.post(f"/api/rooms/{demo['slug']}/members", json={"name": "Asha"})
    assert response.status_code == 409
    assert response.json()["code"] == "GROUP_BUSY"
    assert response.headers["Retry-After"] == "1"
