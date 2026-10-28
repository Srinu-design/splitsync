from collections import Counter

import pytest

from tests.integration.test_edit_delete import (
    expense_id,
)
from tests.integration.test_edit_delete import (
    test_ordered_edit_delete_sequence as run_edit_delete_sequence,
)

pytestmark = pytest.mark.integration


async def test_activity_feed_after_edit_delete_sequence(
    client, engine, demo, integrity, record_testsuite_property
):
    slug = demo["slug"]
    fuel_lineage_id = await expense_id(engine, slug, "Fuel & tolls")

    # DECISION: Replay the ordered edit/delete suite to produce the full verified history sequence.
    await run_edit_delete_sequence(client, engine, demo, integrity, record_testsuite_property)

    # Assert cursor validation & limits
    bad_cursor = await client.get(f"/api/rooms/{slug}/activity?cursor=garbage")
    assert bad_cursor.status_code == 400
    assert bad_cursor.json()["code"] == "INVALID_CURSOR"

    limit_zero = await client.get(f"/api/rooms/{slug}/activity?limit=0")
    assert limit_zero.status_code == 422
    assert limit_zero.json()["code"] == "VALIDATION_ERROR"

    limit_high = await client.get(f"/api/rooms/{slug}/activity?limit=101")
    assert limit_high.status_code == 422
    assert limit_high.json()["code"] == "VALIDATION_ERROR"

    # Fetch full feed with limit=100
    response = await client.get(f"/api/rooms/{slug}/activity?limit=100")
    assert response.status_code == 200
    full_feed = response.json()
    items = full_feed["items"]
    assert full_feed["next_cursor"] is None

    # Count item types
    counts = Counter(item["type"] for item in items)
    assert counts["EXPENSE_ADDED"] == 8
    assert counts["MEMBER_ADDED"] == 5

    # EXPENSE_EDITED for Fuel & tolls with old_total_paise 960000 and total_paise 980000
    fuel_edited = [
        item
        for item in items
        if item["type"] == "EXPENSE_EDITED"
        and item["lineage_id"] == fuel_lineage_id
        and item["old_total_paise"] == 960000
        and item["total_paise"] == 980000
    ]
    assert len(fuel_edited) == 1
    assert "Fuel" in fuel_edited[0]["title"]

    # EXPENSE_DELETED and EXPENSE_RESTORED items
    assert counts["EXPENSE_DELETED"] >= 1
    assert counts["EXPENSE_RESTORED"] >= 1

    # SETTLEMENT_RECORDED and SETTLEMENT_DELETED items
    assert counts["SETTLEMENT_RECORDED"] >= 1
    assert counts["SETTLEMENT_DELETED"] >= 1

    # No item for EDIT reversals
    # Only valid activity feed types are present
    allowed_types = {
        "EXPENSE_ADDED",
        "EXPENSE_EDITED",
        "EXPENSE_DELETED",
        "EXPENSE_RESTORED",
        "SETTLEMENT_RECORDED",
        "SETTLEMENT_DELETED",
        "MEMBER_ADDED",
    }
    assert set(counts.keys()).issubset(allowed_types)
    for item in items:
        assert item.get("reason") != "EDIT"
        assert item.get("parent_reason") != "EDIT" or item["type"] == "EXPENSE_EDITED"

    # Every item has version, at, acted_by; newest first
    for item in items:
        assert "version" in item and isinstance(item["version"], int)
        assert "at" in item and item["at"] is not None
        assert "acted_by" in item

    versions = [item["version"] for item in items]
    assert versions == sorted(versions, reverse=True)

    # Paging with limit=4 following next_cursor returns exactly the same items, in the same order
    paged_items = []
    cursor = None
    while True:
        url = f"/api/rooms/{slug}/activity?limit=4"
        if cursor is not None:
            url += f"&cursor={cursor}"
        page_res = await client.get(url)
        assert page_res.status_code == 200
        page_data = page_res.json()
        paged_items.extend(page_data["items"])
        cursor = page_data["next_cursor"]
        if cursor is None:
            break

    assert paged_items == items

    # lineage=<Fuel lineage id> returns only that expense's items
    fuel_res = await client.get(f"/api/rooms/{slug}/activity?lineage={fuel_lineage_id}&limit=100")
    assert fuel_res.status_code == 200
    fuel_items = fuel_res.json()["items"]
    assert len(fuel_items) > 0
    assert all(item.get("lineage_id") == fuel_lineage_id for item in fuel_items)
    expected_fuel_items = [item for item in items if item.get("lineage_id") == fuel_lineage_id]
    assert fuel_items == expected_fuel_items
