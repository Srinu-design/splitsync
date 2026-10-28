from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.allocation import allocate


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, {3: 3334, 4: 3333, 5: 3333}),
        (1, {3: 3333, 4: 3334, 5: 3333}),
        (2, {3: 3333, 4: 3333, 5: 3334}),
    ],
)
def test_equal_split_rotation(rotation, expected):
    assert allocate(10000, [3, 4, 5], rotation=rotation) == expected


def test_weighted_spec_example():
    assert allocate(240000, [2, 3, 5], [1, 1, 2]) == {2: 60000, 3: 60000, 5: 120000}


@pytest.mark.parametrize(
    ("ids", "weights", "message"),
    [
        ([], None, "no participants"),
        ([1, 2], [1], "one positive weight per member"),
        ([1], [0], "one positive weight per member"),
        ([1], [-1], "one positive weight per member"),
    ],
)
def test_invalid_participants_or_weights(ids, weights, message):
    with pytest.raises(ValueError, match=message):
        allocate(100, ids, weights)


@st.composite
def allocations(draw):
    total = draw(st.integers(min_value=1, max_value=10**8))
    ids = draw(
        st.lists(st.integers(min_value=1, max_value=10000), min_size=1, max_size=50, unique=True)
    )
    weights = draw(
        st.lists(st.integers(min_value=1, max_value=100), min_size=len(ids), max_size=len(ids))
    )
    rotation = draw(st.integers())
    return total, ids, weights, rotation


@settings(max_examples=2000, deadline=None)
@given(allocations())
def test_allocation_conserves_money_and_is_fair(case):
    total, ids, weights, rotation = case
    for split_weights in (weights, None):
        result = allocate(total, ids, split_weights, rotation)
        effective_weights = weights if split_weights is not None else [1] * len(ids)
        assert set(result) == set(ids)
        assert sum(result.values()) == total
        for member, weight in zip(ids, effective_weights, strict=True):
            assert type(result[member]) is int
            assert result[member] >= 0
            assert abs(result[member] - Fraction(total * weight, sum(effective_weights))) < 1
