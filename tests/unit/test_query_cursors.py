import base64
import re
from datetime import date
from pathlib import Path

import pytest

from app.errors import ApiError
from app.services import history, search


def encoded(value):
    return base64.urlsafe_b64encode(value.encode("ascii")).decode("ascii").rstrip("=")


def test_default_cursor_bounds():
    assert history.decode_cursor(None) == (2**63 - 1, 9, 2**63 - 1)
    assert search.decode_cursor(None) == (date.max, 2**63 - 1)


@pytest.mark.parametrize("rank", range(4))
def test_history_cursor_roundtrip(rank):
    cursor = history.encode_cursor(2**63 - 1, rank, 2**63 - 1)
    assert history.decode_cursor(cursor) == (2**63 - 1, rank, 2**63 - 1)
    assert history.decode_cursor(encoded(f"1.{rank}.1")) == (1, rank, 1)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "garbage",
        "a" * 129,
        "MTMuMS4y!",
        "MTMuMS4y===",
        "MTMuMS4y\n",
        encoded("0.1.2"),
        encoded("1.9.2"),
        encoded("1.-1.2"),
        encoded("1.1.0"),
        encoded("01.1.2"),
        encoded("1.1.02"),
        encoded("1.1.2.garbage"),
        encoded(f"{2**63}.1.2"),
        encoded(f"1.1.{2**63}"),
        encoded("1.1.2") + "garbage",
        base64.urlsafe_b64encode(b"\xff").decode("ascii"),
    ],
)
def test_invalid_history_cursor_is_a_400(cursor):
    with pytest.raises(ApiError) as error:
        history.decode_cursor(cursor)
    assert (error.value.status, error.value.code) == (400, "INVALID_CURSOR")


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "garbage",
        "a" * 129,
        "2026-02-30_1",
        "2026-01-01_0",
        "2026-01-01_-1",
        "2026-01-01_01",
        "2026-01-01_1_junk",
        "20260101_1",
        "2026-W01-1_1",
        f"2026-01-01_{2**63}",
        "2026-01-01_1\n",
    ],
)
def test_invalid_search_cursor_is_a_400(cursor):
    with pytest.raises(ApiError) as error:
        search.decode_cursor(cursor)
    assert (error.value.status, error.value.code) == (400, "INVALID_CURSOR")


def test_search_cursor_upper_boundary_and_token_limits():
    assert search.decode_cursor(f"9999-12-31_{2**63 - 1}") == (date.max, 2**63 - 1)
    assert search.search_tokens("Seafood din") == ["seafood", "din"]
    assert search.search_tokens("%_'&|!") == ["_"]
    assert search.search_tokens("A" * 41 + " b c d e f g h i") == [
        "a" * 40,
        "b",
        "c",
        "d",
        "e",
        "f",
        "g",
        "h",
    ]


@pytest.mark.parametrize("q", ["a\x00b", "a\ud800b", "a" * 1001])
def test_search_text_cannot_reach_postgres_with_invalid_encoding(q):
    with pytest.raises(ApiError) as error:
        search.search_tokens(q)
    assert (error.value.status, error.value.code) == (422, "VALIDATION_ERROR")


@pytest.mark.parametrize("value", ["2026-W01-1", "20260101", "0", "2026-02-30", "2026-01-01\x00"])
def test_search_dates_require_extended_iso_date(value):
    with pytest.raises(ApiError) as error:
        search.query_date(value)
    assert (error.value.status, error.value.code) == (422, "VALIDATION_ERROR")


def test_search_filter_values():
    assert search.query_date("2000-01-01") == date(2000, 1, 1)
    assert search.query_date(None) is None
    assert search.query_amount("100.50") == 10050
    assert search.query_amount(None) is None
    with pytest.raises(ApiError) as error:
        search.query_amount("100.505")
    assert (error.value.status, error.value.code) == (422, "INVALID_AMOUNT")


def test_query_sql_is_copied_exactly_from_spec():
    spec = Path("docs/SPEC.md").read_text()
    for heading, sql in (
        ("### History feed", history.HISTORY_SQL),
        ("### Search", search.SEARCH_SQL),
    ):
        block = re.search(r"```sql\n(.*?)```", spec.split(heading, 1)[1], re.S)
        assert sql == block[1]


def test_activity_expense_mapping_has_only_documented_fields():
    data = {
        "at": "2026-10-28T00:00:00+00:00",
        "acted_by": 1,
        "title": "Fuel & tolls",
        "total_paise": 980000,
        "paid_by": 2,
        "lineage_id": 2,
        "revision_of": 2,
        "parent_reason": "EDIT",
        "old_title": "Fuel & tolls",
        "old_total_paise": 960000,
        "spent_on": "2026-10-27",
    }
    row = {"version": 10, "rank": 3, "id": 9, "kind": "EXPENSE", "data": data}
    common = {
        "version": 10,
        "at": data["at"],
        "acted_by": 1,
        "expense_id": 9,
        "lineage_id": 2,
        "title": "Fuel & tolls",
        "total_paise": 980000,
        "paid_by": 2,
        "spent_on": "2026-10-27",
    }
    assert history.activity_item(row) == {
        **common,
        "type": "EXPENSE_EDITED",
        "replaces": 2,
        "old_title": "Fuel & tolls",
        "old_total_paise": 960000,
    }
    assert history.activity_item({**row, "data": {**data, "revision_of": None}}) == {
        **common,
        "type": "EXPENSE_ADDED",
    }
    assert history.activity_item({**row, "data": {**data, "parent_reason": "DELETE"}}) == {
        **common,
        "type": "EXPENSE_RESTORED",
        "restores": 2,
    }


def test_member_history_has_explicit_null_actor():
    assert history.activity_item(
        {"version": 1, "id": 4, "kind": "MEMBER", "data": {"name": "Sneha", "at": "now"}}
    ) == {
        "version": 1,
        "at": "now",
        "acted_by": None,
        "type": "MEMBER_ADDED",
        "member_id": 4,
        "name": "Sneha",
    }


def test_expense_deletion_mapping_does_not_leak_settlement_fields():
    row = {
        "version": 11,
        "id": 2,
        "kind": "REVERSAL",
        "data": {
            "at": "now",
            "acted_by": None,
            "expense_id": 9,
            "lineage_id": 2,
            "title": "Fuel & tolls",
            "total_paise": 980000,
            "settlement_id": None,
            "from": None,
            "to": None,
            "amount_paise": None,
        },
    }
    assert history.activity_item(row) == {
        "type": "EXPENSE_DELETED",
        "version": 11,
        "at": "now",
        "acted_by": None,
        "expense_id": 9,
        "lineage_id": 2,
        "title": "Fuel & tolls",
        "total_paise": 980000,
    }


@pytest.mark.parametrize("kind", ["SETTLEMENT", "REVERSAL"])
def test_settlement_history_mapping(kind):
    data = {
        "at": "now",
        "acted_by": 1,
        "from": 5,
        "to": 1,
        "amount_paise": 500000,
        "mode": "PROTECTED",
        "expense_id": None,
        "settlement_id": 3,
        "title": None,
    }
    item = history.activity_item({"version": 10, "id": 3, "kind": kind, "data": data})
    assert item == {
        "version": 10,
        "at": "now",
        "acted_by": 1,
        "from": 5,
        "to": 1,
        "amount_paise": 500000,
        "settlement_id": 3,
        "type": "SETTLEMENT_RECORDED" if kind == "SETTLEMENT" else "SETTLEMENT_DELETED",
        **({"mode": "PROTECTED"} if kind == "SETTLEMENT" else {}),
    }
