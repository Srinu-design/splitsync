from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.money import InvalidAmount, parse_rupees
from app.errors import ApiError


def no_null_byte(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text must not contain a NUL character")
    value.encode("utf-8")
    return value


MemberName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=40),
    AfterValidator(no_null_byte),
]
RoomName = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80),
    AfterValidator(no_null_byte),
]
MemberId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


def valid_amount(value: str) -> str:
    try:
        parse_rupees(value)
    except InvalidAmount as exc:
        raise ApiError(
            422, "INVALID_AMOUNT", "Invalid amount", "Enter a valid rupee amount."
        ) from exc
    return value


# DECISION: Bound raw amount strings to 32 characters and strip surrounding whitespace only.
Amount = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, max_length=32),
    AfterValidator(valid_amount),
]


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoomIn(RequestModel):
    kind: Annotated[Literal["DEMO", "GROUP"], Field(max_length=5)]
    name: RoomName | None = None
    members: Annotated[list[MemberName], Field(min_length=2, max_length=50)] | None = None

    @model_validator(mode="after")
    def validate_room(self):
        if self.kind == "GROUP":
            if self.name is None or self.members is None:
                raise ValueError("groups require a name and 2–50 members")
            if len({name.lower() for name in self.members}) != len(self.members):
                raise ValueError("member names must be unique ignoring case")
        elif self.name is not None or self.members is not None:
            # DECISION: Demo room names and members come only from the fixed seed.
            raise ValueError("demo rooms do not accept a name or members")
        return self


class MemberIn(RequestModel):
    name: MemberName


class EqualMember(RequestModel):
    id: MemberId


class ShareMember(EqualMember):
    weight: Annotated[int, Field(strict=True, ge=1, le=100)]


class ExactMember(EqualMember):
    amount: Amount


def unique_members(members):
    if len({member.id for member in members}) != len(members):
        raise ValueError("participants must be distinct")
    return members


class EqualSplit(RequestModel):
    type: Annotated[Literal["EQUAL"], Field(max_length=5)]
    members: Annotated[
        list[EqualMember], Field(min_length=1, max_length=50), AfterValidator(unique_members)
    ]


class ShareSplit(RequestModel):
    type: Annotated[Literal["SHARES"], Field(max_length=6)]
    members: Annotated[
        list[ShareMember], Field(min_length=1, max_length=50), AfterValidator(unique_members)
    ]


class ExactSplit(RequestModel):
    type: Annotated[Literal["EXACT"], Field(max_length=5)]
    members: Annotated[
        list[ExactMember], Field(min_length=1, max_length=50), AfterValidator(unique_members)
    ]


class ExpenseIn(RequestModel):
    title: RoomName
    paid_by: MemberId
    amount: Amount
    spent_on: date = Field(default_factory=today)
    split: Annotated[EqualSplit | ShareSplit | ExactSplit, Field(discriminator="type")]

    @field_validator("spent_on", mode="before")
    @classmethod
    def parse_spent_on(cls, value):
        if type(value) is date:
            return value
        if isinstance(value, str) and len(value) == 10:
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise ValueError("spent_on must be an ISO date")

    @field_validator("spent_on")
    @classmethod
    def validate_spent_on(cls, value):
        if not date(2000, 1, 1) <= value <= today() + timedelta(days=1):
            raise ValueError("spent_on must be between 2000-01-01 and tomorrow")
        return value

    @property
    def total_paise(self) -> int:
        return parse_rupees(self.amount)


class SettlementIn(RequestModel):
    from_member: MemberId
    to_member: MemberId
    amount: Amount
    expected_graph_version: Annotated[int, Field(strict=True, gt=0)]
    mode: Annotated[Literal["PROTECTED", "NAIVE"], Field(max_length=9)] = "PROTECTED"

    @model_validator(mode="after")
    def distinct_members(self):
        if self.from_member == self.to_member:
            raise ValueError("settlement members must be different")
        return self

    @property
    def amount_paise(self) -> int:
        return parse_rupees(self.amount)
