# ADR-0007: The "before" graph uses expenses only

Status: Accepted · Date: 2026-10-29

## Context

SplitSync displays two visual debt graphs:
1. **Left ("Before"):** The direct pairwise IOUs incurred by the group.
2. **Right ("After"):** The simplified settlement plan (Greedy or Optimal).

An intuitive initial design would be to update the left graph by subtracting recorded settlements directly from pairwise edges. However, pairwise accounting cannot absorb simplified settlements between participants who do not share a direct debt edge.

For example, in the demo trip, the optimal settlement plan instructs Karthik to pay Aarav ₹5,000. But Karthik never owed Aarav directly—Karthik owed Priya and Rohan, who in turn owed Aarav. If Karthik's ₹5,000 payment to Aarav is recorded against the pairwise graph, it creates a new negative edge (Aarav owes Karthik ₹5,000). After all three optimal payments are executed, the "before" graph still displays 10 directed debt edges forming complex cycles that cancel out per person, confusing users.

## Decision

The left ("Before") graph is computed strictly from active expenses, completely ignoring settlements:

- In [`app/domain/simplify.py pairwise_edges()`](../../app/domain/simplify.py) and [`app/services/state.py OWED_SQL`](../../app/services/state.py), pairwise edges are derived exclusively by aggregating splits from `active_expenses`.
- The left graph represents **historical expenses as incurred**. It shows the raw, un-simplified debt web created by the group's spending.
- The right ("After") graph represents **what remains to be paid**, calculated from each member's current `net_balance_paise` (which incorporates both expenses and settlements).
- When a settlement is recorded, the right graph shrinks toward zero, while the left graph remains stable as a reference of original spending obligations.

## Alternatives considered

- **Subtracting settlements pairwise from the left graph.** Produces phantom cyclic arrows and negative debt inversions when members pay optimal cross-transfers.
- **Hiding the left graph once any settlement is recorded.** Deprives users of the visual comparison showing how debt simplification reduced their web of IOUs.

## Consequences

- The visual distinction is clear:
  - Left graph: "What we spent and who owed whom directly" (10 edges on demo trip).
  - Right graph: "The minimum payments needed to square everyone up right now" (3 optimal transfers).
- Settling a debt cleanly reduces the arrows on the right graph until it completely empties when all balances reach ₹0.00.
- Validated in [`tests/unit/test_simplify.py`](../../tests/unit/test_simplify.py).

## When we would revisit

If supporting multi-party settlement contracts where specific expenses are explicitly marked as "settled item by item" rather than settled against the overall pool.
