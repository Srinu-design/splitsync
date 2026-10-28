from collections import defaultdict

import pytest

from app.domain.allocation import allocate
from app.seed_data import EXPENSES, MEMBERS


@pytest.fixture
def seed():
    member_ids = {name: member for member, (name, _) in enumerate(MEMBERS, start=1)}
    balances = dict.fromkeys(member_ids.values(), 0)
    owed = defaultdict(int)
    expenses = []
    for expense_id, (_, payer, total, split_type, participants, weights, _) in enumerate(
        EXPENSES, start=1
    ):
        paid_by = member_ids[payer]
        ids = [member_ids[name] for name in participants]
        if split_type == "EXACT":
            shares = {member_ids[name]: amount for name, amount in weights.items()}
        else:
            shares = allocate(total, ids, weights, rotation=expense_id)
        # Compute the fixture independently of the ledger helpers under test.
        balances[paid_by] += total
        for member, share in shares.items():
            balances[member] -= share
            if member != paid_by and share > 0:
                owed[member, paid_by] += share
        expenses.append((paid_by, total, shares))
    return {"balances": balances, "owed": dict(owed), "expenses": expenses}
