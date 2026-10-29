# ADR-0001: Integer paise and largest-remainder splits

Status: Accepted · Date: 2026-10-29

## Context

Financial group splitters calculate cost shares among members. Using IEEE 754 floating-point numbers (`float`) introduces precision issues: `0.1 + 0.2 != 0.3`, and splitting ₹100.00 among three people results in ₹33.333333333333336 per person. Rounding naive floats to two decimal places yields ₹33.33 each, losing 1 paisa (₹0.01) from the total. Over time, member balances stop summing to zero, violating basic double-entry bookkeeping rules.

Even with arbitrary-precision decimals, dividing uneven amounts leaves remainder paise that must be assigned without bias.

## Decision

Store all monetary amounts as 64-bit signed integers (`BIGINT`) representing Indian paise (1 Rupee = 100 paise). Capped at ₹10,00,000.00 (`MAX_PAISE = 100_000_000`).

- Rupee strings from API requests (e.g., `"100.50"`) are parsed with Python's `Decimal` in [`app/domain/money.py`](../../app/domain/money.py) and immediately converted to integer paise. Floats are never used in financial calculations or stored in the database.
- Split allocations implement the **Hamilton largest remainder method** in [`app/domain/allocation.py`](../../app/domain/allocation.py):
  1. Each member receives their integer floor share: $\lfloor \text{total} \times w_m / \sum w \rfloor$.
  2. Fractional remainders are computed: $(\text{total} \times w_m) \pmod{\sum w}$.
  3. Leftover paise are distributed one by one to participants with the highest fractional remainders.
- Tie-breaking across equal remainders rotates based on `rotation = expense_id % len(participants)`. This guarantees that no single participant systematically bears leftover rounding paise over successive expenses.

## Alternatives considered

- **Binary floats (`FLOAT` / `DOUBLE PRECISION`).** Simple, but causes rounding drift and breaks the invariant that member balances sum to zero.
- **PostgreSQL `NUMERIC` / `DECIMAL`.** Exact, but money travels as decimal strings through every layer, and it still does not decide who gets the indivisible leftover paisa.
- **Fixed tie-breaking by member name or ID.** Leads to systematic unfairness where member ID 1 always pays the rounding discrepancy on equal splits.

## Consequences

- Balances across all room members are guaranteed to sum to exactly `0` paise at all times (`sum(net_balance_paise) == 0`).
- Every member's share is within 1 paisa of their mathematically exact proportion.
- Checked by a Hypothesis property test over 2,000 random splits in [`tests/unit/test_allocation.py`](../../tests/unit/test_allocation.py) and string validation tests in [`tests/unit/test_money.py`](../../tests/unit/test_money.py).

## When we would revisit

If supporting currencies with fractional-cent units or sub-cent micropayments, or currencies without decimal subdivisions (e.g., Japanese Yen).
