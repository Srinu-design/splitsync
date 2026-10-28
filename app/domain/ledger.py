def expense_deltas(paid_by: int, total: int, shares: dict[int, int]) -> dict[int, int]:
    # DECISION: Merge payer and participant effects, dropping zero balances consistently.
    return merge({paid_by: total}, negate(shares))


def negate(deltas: dict[int, int]) -> dict[int, int]:
    # DECISION: Reversals negate only nonzero entries.
    return {member: -amount for member, amount in deltas.items() if amount}


def merge(*deltas: dict[int, int]) -> dict[int, int]:
    # DECISION: Sum changes by member and omit zeros; callers control write ordering.
    combined: dict[int, int] = {}
    for delta in deltas:
        for member, amount in delta.items():
            combined[member] = combined.get(member, 0) + amount
    return {member: amount for member, amount in combined.items() if amount}


def settlement_deltas(from_member: int, to_member: int, amount: int) -> dict[int, int]:
    # DECISION: Merge both settlement effects so a same-member pair cancels naturally.
    return merge({from_member: amount}, {to_member: -amount})


def integrity_report(
    rows: list[tuple[int, int, int]], bad_split_expenses: int, orphan_edits: int
) -> dict:
    # DECISION: Return the inner invariants object, with drift entries ordered by member id.
    drifted = [
        {
            "member_id": member,
            "cached_paise": cached,
            "ledger_paise": ledger,
            "drift_paise": cached - ledger,
        }
        for member, cached, ledger in sorted(rows)
        if cached != ledger
    ]
    return {
        "zero_sum_paise": sum(cached for _, cached, _ in rows),
        "ledger_drift_total_paise": sum(abs(row["drift_paise"]) for row in drifted),
        "drifted_members": drifted,
        "bad_split_expenses": bad_split_expenses,
        "orphan_edits": orphan_edits,
    }
