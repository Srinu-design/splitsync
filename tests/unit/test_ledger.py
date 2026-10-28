from app.domain.ledger import (
    expense_deltas,
    integrity_report,
    merge,
    negate,
    settlement_deltas,
)


def test_expense_deltas_reproduce_seed_balances_and_reverse(seed):
    deltas = [expense_deltas(payer, total, shares) for payer, total, shares in seed["expenses"]]
    combined = merge(*deltas)
    assert combined == seed["balances"]
    assert combined == {1: 500000, 2: 350000, 3: 250000, 4: -600000, 5: -500000}
    assert sum(combined.values()) == 0
    assert merge(*[negate(delta) for delta in deltas]) == negate(combined)
    assert merge(combined, negate(combined)) == {}


def test_expense_deltas_merge_payer_share_without_mutating_it():
    shares = {1: 30, 2: 70}
    assert expense_deltas(1, 100, shares) == {1: 70, 2: -70}
    assert shares == {1: 30, 2: 70}
    assert expense_deltas(1, 100, {1: 100}) == {}


def test_expense_payer_can_be_outside_participants():
    assert expense_deltas(3, 100, {1: 30, 2: 70}) == {1: -30, 2: -70, 3: 100}


def test_merge_and_negate_drop_zeros_and_handle_empty_inputs():
    changes = {1: 20, 2: -20, 3: 0}
    assert negate(changes) == {1: -20, 2: 20}
    assert merge(changes, {1: -20, 2: 25, 4: 0}) == {2: 5}
    assert changes == {1: 20, 2: -20, 3: 0}
    assert merge() == {}
    assert merge({}) == {}
    assert negate({}) == {}


def test_settlement_signs_and_reversal():
    deltas = settlement_deltas(5, 1, 500000)
    assert deltas == {5: 500000, 1: -500000}
    assert sum(deltas.values()) == 0
    assert merge(deltas, negate(deltas)) == {}
    assert merge(deltas, settlement_deltas(1, 5, 500000)) == {}


def test_integrity_report_on_clean_seed(seed):
    rows = [(member, amount, amount) for member, amount in seed["balances"].items()]
    assert integrity_report(rows, 0, 0) == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 0,
        "drifted_members": [],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }


def test_integrity_report_exposes_naive_drift_in_member_order(seed):
    payment = settlement_deltas(5, 1, 500000)
    cached = {**seed["balances"], 1: 0, 5: 0}
    ledger = merge(seed["balances"], *[payment] * 10)
    rows = [(member, cached[member], ledger[member]) for member in reversed(seed["balances"])]
    assert integrity_report(rows, 0, 0) == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 9000000,
        "drifted_members": [
            {"member_id": 1, "cached_paise": 0, "ledger_paise": -4500000, "drift_paise": 4500000},
            {"member_id": 5, "cached_paise": 0, "ledger_paise": 4500000, "drift_paise": -4500000},
        ],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }


def test_integrity_report_preserves_counts_and_nonzero_sum():
    assert integrity_report([(2, 12, 12), (1, -5, -5)], 2, 3) == {
        "zero_sum_paise": 7,
        "ledger_drift_total_paise": 0,
        "drifted_members": [],
        "bad_split_expenses": 2,
        "orphan_edits": 3,
    }


def test_integrity_report_on_empty_ledger():
    assert integrity_report([], 0, 0) == {
        "zero_sum_paise": 0,
        "ledger_drift_total_paise": 0,
        "drifted_members": [],
        "bad_split_expenses": 0,
        "orphan_edits": 0,
    }
