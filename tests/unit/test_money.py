import pytest

from app.domain.money import MAX_PAISE, InvalidAmount, parse_rupees


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("100", 10000),
        ("100.5", 10050),
        ("100.50", 10050),
        ("0.01", 1),
        ("999999.99", 99999999),
        ("1000000", 100000000),
        ("1000000.00", MAX_PAISE),
        (" \t100.50\n", 10050),
    ],
)
def test_parse_rupees_accepts(amount, expected):
    result = parse_rupees(amount)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize(
    "amount",
    [
        "100.505",
        "-5",
        "1e3",
        "",
        "abc",
        "01",
        "0",
        "0.00",
        "1000000.01",
        "10000000",
        "00.01",
        "1.",
    ],
)
def test_parse_rupees_rejects(amount):
    with pytest.raises(InvalidAmount):
        parse_rupees(amount)
