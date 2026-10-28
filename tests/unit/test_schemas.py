from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.errors import ApiError, register_error_handlers
from app.schemas import (
    EqualSplit,
    ExactMember,
    ExactSplit,
    ExpenseIn,
    MemberIn,
    RoomIn,
    SettlementIn,
    ShareSplit,
    today,
)


def expense_body(**changes):
    return {
        "title": "Airport auto",
        "paid_by": 3,
        "amount": "100",
        "split": {"type": "EQUAL", "members": [{"id": 3}, {"id": 4}, {"id": 5}]},
        **changes,
    }


def settlement_body(**changes):
    return {
        "from_member": 5,
        "to_member": 1,
        "amount": "5000",
        "expected_graph_version": 9,
        **changes,
    }


@pytest.mark.parametrize("count", [2, 50])
def test_group_names_are_trimmed_and_accept_member_caps(count):
    names = [f" Member {index} " for index in range(count)]
    room = RoomIn(kind="GROUP", name=" Flat 4B ", members=names)
    assert room.name == "Flat 4B"
    assert room.members == [name.strip() for name in names]
    assert RoomIn(kind="DEMO").members is None
    assert MemberIn(name=" Asha ").name == "Asha"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "GROUP"},
        {"kind": "GROUP", "name": "Flat"},
        {"kind": "GROUP", "members": ["Asha", "Ravi"]},
        {"kind": "GROUP", "name": "Flat", "members": ["Asha"]},
        {"kind": "GROUP", "name": "Flat", "members": [f"M{i}" for i in range(51)]},
        {"kind": "GROUP", "name": "Flat", "members": [" Asha ", "asha"]},
        {"kind": "GROUP", "name": " ", "members": ["Asha", "Ravi"]},
        {"kind": "GROUP", "name": "x" * 81, "members": ["Asha", "Ravi"]},
        {"kind": "GROUP", "name": "Flat", "members": ["x" * 41, "Ravi"]},
        {"kind": "GROUP", "name": "Flat", "members": ["", "Ravi"]},
        {"kind": "GROUP", "name": "Flat", "members": [1, "Ravi"]},
        {"kind": "DEMO", "name": "Custom"},
        {"kind": "DEMO", "members": ["Asha", "Ravi"]},
        {"kind": "OTHER"},
        {"kind": "DEMO", "extra": True},
    ],
)
def test_invalid_room_bodies(body):
    with pytest.raises(ValidationError):
        RoomIn.model_validate(body)


@pytest.mark.parametrize("name", ["", " ", "x" * 41, 1, None])
def test_member_name_limits_and_types(name):
    with pytest.raises(ValidationError):
        MemberIn(name=name)


@pytest.mark.parametrize(
    ("split", "expected_type"),
    [
        ({"type": "EQUAL", "members": [{"id": 3}]}, EqualSplit),
        ({"type": "SHARES", "members": [{"id": 3, "weight": 100}]}, ShareSplit),
        ({"type": "EXACT", "members": [{"id": 3, "amount": " 100.00 "}]}, ExactSplit),
    ],
)
def test_expense_split_variants_and_normalized_hash_body(split, expected_type):
    body = ExpenseIn.model_validate(
        expense_body(title=" Airport auto ", amount=" 100.00 ", split=split)
    )
    assert isinstance(body.split, expected_type)
    assert body.title == "Airport auto"
    assert body.amount == "100.00"
    assert body.total_paise == 10000
    assert body.spent_on == today()
    dumped = body.model_dump(mode="json")
    assert dumped["amount"] == "100.00"
    assert dumped["spent_on"] == today().isoformat()
    assert "total_paise" not in dumped
    if expected_type is ExactSplit:
        assert body.split.members[0].amount == "100.00"


@pytest.mark.parametrize("model", [ExpenseIn, SettlementIn, ExactMember])
@pytest.mark.parametrize(
    "amount", ["100.505", "-5", "1e3", "", "abc", "01", "0", "0.00", "1000000.01"]
)
def test_bad_amount_strings_have_invalid_amount_code(model, amount):
    fields = (
        expense_body(amount=amount)
        if model is ExpenseIn
        else settlement_body(amount=amount)
        if model is SettlementIn
        else {"id": 1, "amount": amount}
    )
    with pytest.raises(ApiError) as caught:
        model.model_validate(fields)
    assert caught.value.status == 422
    assert caught.value.code == "INVALID_AMOUNT"


@pytest.mark.parametrize("amount", [1, True, None, [], {}, "1" * 33])
def test_amount_types_and_string_bound(amount):
    with pytest.raises(ValidationError):
        ExpenseIn.model_validate(expense_body(amount=amount))


def test_maximum_amount_and_settlement_defaults():
    body = SettlementIn.model_validate(settlement_body(amount="1000000.00"))
    assert body.mode == "PROTECTED"
    assert body.amount_paise == 100000000
    assert "amount_paise" not in body.model_dump()


@pytest.mark.parametrize("day", [date(2000, 1, 1), today(), today() + timedelta(days=1)])
def test_spent_on_date_boundaries(day):
    body = ExpenseIn.model_validate(expense_body(spent_on=day.isoformat()))
    assert body.spent_on == day


@pytest.mark.parametrize(
    "day",
    [
        "1999-12-31",
        (today() + timedelta(days=2)).isoformat(),
        "2026-13-01",
        "2026-01-01T00:00:00",
        "not a date",
        0,
        None,
        datetime(2026, 1, 1),
    ],
)
def test_invalid_spent_on(day):
    with pytest.raises(ValidationError):
        ExpenseIn.model_validate(expense_body(spent_on=day))


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"title": "x" * 81},
        {"title": 1},
        {"paid_by": True},
        {"paid_by": "3"},
        {"paid_by": 0},
        {"extra": "unexpected"},
    ],
)
def test_expense_field_validation(changes):
    with pytest.raises(ValidationError):
        ExpenseIn.model_validate(expense_body(**changes))


@pytest.mark.parametrize(
    "split",
    [
        {"type": "UNKNOWN", "members": [{"id": 1}]},
        {"type": "EQUAL", "members": []},
        {"type": "EQUAL", "members": [{"id": i} for i in range(1, 52)]},
        {"type": "EQUAL", "members": [{"id": 1}, {"id": 1}]},
        {"type": "SHARES", "members": [{"id": 1, "weight": 1}, {"id": 1, "weight": 2}]},
        {"type": "EXACT", "members": [{"id": 1, "amount": "1"}, {"id": 1, "amount": "2"}]},
        {"type": "EQUAL", "members": [{"id": 1, "weight": 1}]},
        {"type": "EQUAL", "members": [{"id": True}]},
        {"type": "EQUAL", "members": [{"id": "1"}]},
        {"type": "EQUAL", "members": [{"id": -1}]},
        {"type": "EQUAL", "members": [{"id": 1}], "extra": True},
        {"type": "SHARES", "members": [{"id": 1, "weight": 0}]},
        {"type": "SHARES", "members": [{"id": 1, "weight": 101}]},
        {"type": "SHARES", "members": [{"id": 1, "weight": True}]},
        {"type": "SHARES", "members": [{"id": 1, "weight": "1"}]},
        {"type": "EXACT", "members": [{"id": 1, "amount": "1", "weight": 1}]},
    ],
)
def test_invalid_splits(split):
    with pytest.raises(ValidationError):
        ExpenseIn.model_validate(expense_body(split=split))


@pytest.mark.parametrize(
    "changes",
    [
        {"from_member": 1},
        {"from_member": True},
        {"to_member": "1"},
        {"to_member": 0},
        {"expected_graph_version": True},
        {"expected_graph_version": "9"},
        {"expected_graph_version": 0},
        {"mode": "OTHER"},
        {"extra": True},
    ],
)
def test_invalid_settlement_fields(changes):
    with pytest.raises(ValidationError):
        SettlementIn.model_validate(settlement_body(**changes))


@pytest.mark.parametrize("member_id", [2**63, 10**100])
def test_member_ids_must_fit_postgres_bigint(member_id):
    with pytest.raises(ValidationError):
        ExpenseIn.model_validate(expense_body(paid_by=member_id))
    for member_field in ("from_member", "to_member"):
        with pytest.raises(ValidationError):
            SettlementIn.model_validate(settlement_body(**{member_field: member_id}))
    for split_type, fields in (
        ("EQUAL", {}),
        ("SHARES", {"weight": 1}),
        ("EXACT", {"amount": "1"}),
    ):
        split = {"type": split_type, "members": [{"id": member_id, **fields}]}
        with pytest.raises(ValidationError):
            ExpenseIn.model_validate(expense_body(split=split))


def test_member_ids_accept_postgres_bigint_maximum():
    member_id = 2**63 - 1
    body = ExpenseIn.model_validate(
        expense_body(paid_by=member_id, split={"type": "EQUAL", "members": [{"id": member_id}]})
    )
    assert body.paid_by == body.split.members[0].id == member_id
    assert (
        SettlementIn.model_validate(settlement_body(from_member=member_id)).from_member == member_id
    )


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (MemberIn, {"name": "Asha\x00"}),
        (RoomIn, {"kind": "GROUP", "name": "Flat\x00", "members": ["Asha", "Ravi"]}),
        (RoomIn, {"kind": "GROUP", "name": "Flat", "members": ["Asha\x00", "Ravi"]}),
        (ExpenseIn, expense_body(title="Airport\x00auto")),
        (MemberIn, {"name": "\ud800"}),
        (ExpenseIn, expense_body(title="\udfff")),
    ],
)
def test_postgres_text_fields_reject_invalid_text(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.fixture
def validation_app():
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/expenses")
    async def expense(body: ExpenseIn):
        return body.model_dump(mode="json")

    @app.get("/busy")
    async def busy():
        raise ApiError(
            409,
            "GROUP_BUSY",
            "Room busy",
            "Try again shortly.",
            headers={"Retry-After": "1"},
            current_graph_version=9,
        )

    return app


async def test_problem_error_format_and_headers(validation_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_app), base_url="http://test"
    ) as client:
        response = await client.get("/busy")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "type": "/errors/group-busy",
        "title": "Room busy",
        "status": 409,
        "code": "GROUP_BUSY",
        "detail": "Try again shortly.",
        "current_graph_version": 9,
    }


@pytest.mark.parametrize(
    ("changes", "code"),
    [({"paid_by": True}, "VALIDATION_ERROR"), ({"amount": "nope"}, "INVALID_AMOUNT")],
)
async def test_request_validation_and_amount_errors_use_problem_shape(
    validation_app, changes, code
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_app), base_url="http://test"
    ) as client:
        response = await client.post("/expenses", json=expense_body(**changes))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code
    assert response.json()["type"] == f"/errors/{code.lower().replace('_', '-')}"
    assert response.json()["status"] == 422
    assert isinstance(response.json()["title"], str)
    assert isinstance(response.json()["detail"], str)
