import pytest
from sqlalchemy import text

from tests.integration.test_edit_delete import expense_id

pytestmark = pytest.mark.integration


async def test_search_queries_and_filters_on_fresh_demo_room(client, engine, demo):
    slug = demo["slug"]

    # q="vil" -> only Villa booking
    res_vil = await client.get(f"/api/rooms/{slug}/expenses?q=vil")
    assert res_vil.status_code == 200
    items_vil = res_vil.json()["items"]
    assert len(items_vil) == 1
    assert items_vil[0]["title"] == "Villa booking (2 nights)"

    # q="seafood din" -> only Beach-shack seafood dinner
    res_sea = await client.get(f"/api/rooms/{slug}/expenses?q=seafood+din")
    assert res_sea.status_code == 200
    items_sea = res_sea.json()["items"]
    assert len(items_sea) == 1
    assert items_sea[0]["title"] == "Beach-shack seafood dinner"

    # paid_by filter
    aarav_id = demo["members"]["Aarav"]
    res_paid = await client.get(f"/api/rooms/{slug}/expenses?paid_by={aarav_id}")
    assert res_paid.status_code == 200
    items_paid = res_paid.json()["items"]
    assert len(items_paid) == 2
    assert {item["title"] for item in items_paid} == {"Villa booking (2 nights)", "Parasailing"}
    assert all(item["paid_by"] == aarav_id for item in items_paid)

    # member filter (participants)
    priya_id = demo["members"]["Priya"]
    res_member = await client.get(f"/api/rooms/{slug}/expenses?member={priya_id}")
    assert res_member.status_code == 200
    items_member = res_member.json()["items"]
    # Priya participated in all expenses except Parasailing (7 out of 8)
    assert len(items_member) == 7
    assert "Parasailing" not in {item["title"] for item in items_member}

    # min/max rupee strings
    res_min = await client.get(f"/api/rooms/{slug}/expenses?min=5000")
    assert res_min.status_code == 200
    items_min = res_min.json()["items"]
    assert len(items_min) == 5
    assert all(item["total_paise"] >= 500000 for item in items_min)

    res_max = await client.get(f"/api/rooms/{slug}/expenses?max=5000")
    assert res_max.status_code == 200
    items_max = res_max.json()["items"]
    assert len(items_max) == 4
    assert all(item["total_paise"] <= 500000 for item in items_max)

    res_range = await client.get(f"/api/rooms/{slug}/expenses?min=4000&max=6000")
    assert res_range.status_code == 200
    items_range = res_range.json()["items"]
    assert len(items_range) == 3
    assert {item["title"] for item in items_range} == {
        "Parasailing",
        "Club night",
        "Dudhsagar jeep safari",
    }

    # from/to date filters
    async with engine.connect() as connection:
        dates = dict(
            (
                await connection.execute(
                    text("""
            SELECT e.title, e.spent_on FROM expenses e
            JOIN groups g ON g.id = e.group_id WHERE g.slug = :slug
        """),
                    {"slug": slug},
                )
            ).all()
        )

    # Dudhsagar is 1 day ago; filtering from that date gets expenses from days 1 and 0 (4 rows)
    date_dudhsagar = dates["Dudhsagar jeep safari"].isoformat()
    res_from = await client.get(f"/api/rooms/{slug}/expenses?from={date_dudhsagar}")
    assert res_from.status_code == 200
    items_from = res_from.json()["items"]
    assert len(items_from) == 4
    assert {item["title"] for item in items_from} == {
        "Dudhsagar jeep safari",
        "Parasailing",
        "Cafe brunch",
        "Club night",
    }

    # Beach-shack is 2 days ago; filtering to that date gets expenses from days 3 and 2 (4 expenses)
    date_beach = dates["Beach-shack seafood dinner"].isoformat()
    res_to = await client.get(f"/api/rooms/{slug}/expenses?to={date_beach}")
    assert res_to.status_code == 200
    items_to = res_to.json()["items"]
    assert len(items_to) == 4
    assert {item["title"] for item in items_to} == {
        "Villa booking (2 nights)",
        "Fuel & tolls",
        "Beach-shack seafood dinner",
        "Scooter rentals",
    }

    # Exact date filter (both from and to set to 2 days ago)
    res_exact_date = await client.get(
        f"/api/rooms/{slug}/expenses?from={date_beach}&to={date_beach}"
    )
    assert res_exact_date.status_code == 200
    items_exact_date = res_exact_date.json()["items"]
    assert len(items_exact_date) == 2
    assert {item["title"] for item in items_exact_date} == {
        "Beach-shack seafood dinner",
        "Scooter rentals",
    }

    # Paging with limit=3 equals one big page
    res_all = await client.get(f"/api/rooms/{slug}/expenses?limit=100")
    assert res_all.status_code == 200
    all_items = res_all.json()["items"]
    assert len(all_items) == 8

    paged_items = []
    cursor = None
    while True:
        url = f"/api/rooms/{slug}/expenses?limit=3"
        if cursor is not None:
            url += f"&cursor={cursor}"
        page_res = await client.get(url)
        assert page_res.status_code == 200
        page_data = page_res.json()
        paged_items.extend(page_data["items"])
        cursor = page_data["next_cursor"]
        if cursor is None:
            break

    assert paged_items == all_items

    # q="%_'&|!" -> 200 with no error
    # DECISION: Escaped query characters should evaluate safely without raising errors.
    res_special = await client.get(f"/api/rooms/{slug}/expenses", params={"q": "%_'&|!"})
    assert res_special.status_code == 200

    # Bad cursor -> 400 INVALID_CURSOR
    res_bad_cursor = await client.get(f"/api/rooms/{slug}/expenses?cursor=garbage")
    assert res_bad_cursor.status_code == 400
    assert res_bad_cursor.json()["code"] == "INVALID_CURSOR"

    # min="abc" -> 422 INVALID_AMOUNT
    res_bad_min = await client.get(f"/api/rooms/{slug}/expenses?min=abc")
    assert res_bad_min.status_code == 422
    assert res_bad_min.json()["code"] == "INVALID_AMOUNT"


async def test_search_delete_and_restore_visibility(client, engine, demo):
    slug = demo["slug"]
    target_id = await expense_id(engine, slug, "Parasailing")

    # Delete an expense
    del_res = await client.delete(
        f"/api/rooms/{slug}/expenses/{target_id}",
        headers={"Idempotency-Key": "search-test-delete-001"},
    )
    assert del_res.status_code == 200

    # Hidden by default
    res_default = await client.get(f"/api/rooms/{slug}/expenses")
    assert res_default.status_code == 200
    default_items = res_default.json()["items"]
    assert len(default_items) == 7
    assert target_id not in [item["expense_id"] for item in default_items]
    assert all(item["deleted"] is False for item in default_items)

    # With include_deleted=true it appears with deleted: true
    res_with_del = await client.get(f"/api/rooms/{slug}/expenses?include_deleted=true")
    assert res_with_del.status_code == 200
    all_items = res_with_del.json()["items"]
    assert len(all_items) == 8
    deleted_row = next(item for item in all_items if item["expense_id"] == target_id)
    assert deleted_row["title"] == "Parasailing"
    assert deleted_row["deleted"] is True

    # Restore the expense to leave room clean
    restore_res = await client.post(
        f"/api/rooms/{slug}/expenses/{target_id}/restore",
        headers={"Idempotency-Key": "search-test-restore-001"},
    )
    assert restore_res.status_code == 201
