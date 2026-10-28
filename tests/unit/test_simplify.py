import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.domain.simplify import exact, greedy, pairwise_edges, plans
from app.seed_data import EXPENSES, MEMBERS, ROOM_NAME


def test_seed_initial_state(seed):
    assert ROOM_NAME == "Goa Road Trip"
    assert [name for name, _ in MEMBERS] == ["Aarav", "Priya", "Rohan", "Sneha", "Karthik"]
    assert 1 + len(EXPENSES) == 9  # Initial graph version plus the eight seed writes.
    assert seed["balances"] == {1: 500000, 2: 350000, 3: 250000, 4: -600000, 5: -500000}
    assert all(sum(shares.values()) == total for _, total, shares in seed["expenses"])
    assert seed["expenses"][6][2] == {2: 60000, 3: 60000, 5: 120000}
    assert seed["expenses"][7][2] == {1: 120000, 2: 90000, 3: 100000, 4: 80000, 5: 110000}


def test_seed_pairwise_edges(seed):
    edges = pairwise_edges(seed["balances"], seed["owed"])
    assert len(edges) == 10
    # Reference iteration follows member pairs; the spec table sorts amounts instead.
    assert set(edges) == {
        (4, 1, 288000),
        (5, 1, 238000),
        (5, 2, 196000),
        (4, 3, 136000),
        (4, 2, 126000),
        (5, 3, 116000),
        (4, 5, 50000),
        (3, 2, 40000),
        (1, 3, 38000),
        (2, 1, 12000),
    }


def test_seed_greedy_plan_in_order(seed):
    edges, steps = greedy(seed["balances"])
    assert edges == [(4, 1, 500000), (5, 2, 350000), (5, 3, 150000), (4, 3, 100000)]
    assert steps == [
        {
            "debtor": 4,
            "debt_paise": 600000,
            "creditor": 1,
            "credit_paise": 500000,
            "amount_paise": 500000,
        },
        {
            "debtor": 5,
            "debt_paise": 500000,
            "creditor": 2,
            "credit_paise": 350000,
            "amount_paise": 350000,
        },
        {
            "debtor": 5,
            "debt_paise": 150000,
            "creditor": 3,
            "credit_paise": 250000,
            "amount_paise": 150000,
        },
        {
            "debtor": 4,
            "debt_paise": 100000,
            "creditor": 3,
            "credit_paise": 100000,
            "amount_paise": 100000,
        },
    ]


def test_seed_optimal_plan_and_chaos_target(seed):
    edges, groups = exact(seed["balances"])
    assert edges == [(4, 2, 350000), (4, 3, 250000), (5, 1, 500000)]
    assert groups == [[4, 3, 2], [5, 1]]
    assert min(edges, key=lambda edge: (-edge[2], edge[0], edge[1])) == (5, 1, 500000)
    assert plans(seed["balances"]) == {
        "nonzero_members": 5,
        "greedy": {
            "count": 4,
            "edges": greedy(seed["balances"])[0],
            "steps": greedy(seed["balances"])[1],
        },
        "optimal": {"count": 3, "edges": edges, "groups": groups},
        "recommended": "OPTIMAL",
    }


def test_greedy_counterexample():
    balances = {1: 4, 2: 3, 3: 3, 4: -6, 5: -4}
    assert len(greedy(balances)[0]) == 4
    assert len(exact(balances)[0]) == 3


def partitions(values):
    """Enumerate set partitions independently of the reference solver's bitmask DP."""
    if not values:
        yield []
        return
    first, *rest = values
    for partition in partitions(rest):
        yield [(first,), *partition]
        for index, group in enumerate(partition):
            yield [*partition[:index], (first, *group), *partition[index + 1 :]]


def brute_force_minimum(balances):
    values = [balance for balance in balances.values() if balance]
    most_groups = max(
        len(partition)
        for partition in partitions(values)
        if all(sum(group) == 0 for group in partition)
    )
    return len(values) - most_groups


@st.composite
def zero_sum_balances(draw):
    values = draw(st.lists(st.integers(min_value=-(10**8), max_value=10**8), max_size=6))
    if not values:
        return {}
    values.append(-sum(values))
    ids = draw(
        st.lists(
            st.integers(min_value=1, max_value=1000),
            min_size=len(values),
            max_size=len(values),
            unique=True,
        )
    )
    return dict(zip(ids, values, strict=True))


@settings(max_examples=300, deadline=None)
@given(zero_sum_balances())
@example({})
@example({1: 0})
@example({1: 4, 2: 3, 3: 3, 4: -6, 5: -4})
def test_plans_settle_every_balance_with_minimum_transfers(balances):
    greedy_edges, _ = greedy(balances)
    exact_edges, groups = exact(balances)
    for edges in (greedy_edges, exact_edges):
        remaining = balances.copy()
        for debtor, creditor, amount in edges:
            assert debtor != creditor
            assert type(amount) is int and amount > 0
            remaining[debtor] += amount
            remaining[creditor] -= amount
        assert all(balance == 0 for balance in remaining.values())
    nonzero_ids = {member for member, balance in balances.items() if balance}
    assert len(exact_edges) <= len(greedy_edges) <= max(0, len(nonzero_ids) - 1)
    assert len(exact_edges) == brute_force_minimum(balances)
    grouped = [member for group in groups for member in group]
    assert len(grouped) == len(nonzero_ids)
    assert set(grouped) == nonzero_ids
    assert all(sum(balances[member] for member in group) == 0 for group in groups)


def test_large_group_uses_greedy():
    balances = {member: 1 if member <= 8 else -1 for member in range(1, 17)}
    balances[17] = 0
    result = plans(balances)
    assert result["nonzero_members"] == 16
    assert result["optimal"] is None
    assert result["recommended"] == "GREEDY"
    assert result["greedy"]["count"] == len(result["greedy"]["edges"]) == 8


def test_greedy_rejects_unbalanced_input():
    with pytest.raises(ValueError, match="balances do not sum to zero"):
        greedy({1: 1, 2: -2})


def test_pairwise_nets_both_directions_and_omits_zero():
    assert pairwise_edges([3, 1, 2], {(1, 2): 10, (2, 1): 3, (1, 3): 4, (3, 1): 4}) == [(1, 2, 7)]
