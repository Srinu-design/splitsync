# ADR-0006: Greedy plus exact bitmask DP, exact up to 15 non-zero balances

Status: Accepted · Date: 2026-10-29

## Context

Group expense splitters aim to simplify a web of debts into the minimum number of payments. A two-heap greedy algorithm runs in $O(N \log N)$ and guarantees at most $N - 1$ transfers by repeatedly pairing the largest debtor with the largest creditor.

However, greedy is not optimal. For example, on the seeded "Goa Road Trip" trip (5 members, net balances: Aarav +₹5,000, Priya +₹3,500, Rohan +₹2,500, Sneha −₹6,000, Karthik −₹5,000), greedy produces 4 transfers:
1. Sneha $\rightarrow$ Aarav: ₹5,000
2. Karthik $\rightarrow$ Priya: ₹3,500
3. Karthik $\rightarrow$ Rohan: ₹1,500
4. Sneha $\rightarrow$ Rohan: ₹1,000

The true mathematical optimum is 3 transfers:
1. Karthik $\rightarrow$ Aarav: ₹5,000
2. Sneha $\rightarrow$ Priya: ₹3,500
3. Sneha $\rightarrow$ Rohan: ₹2,500

Finding the absolute minimum number of transfers requires partitioning members into the maximum number of disjoint zero-sum subsets ($S_{\max}$). The minimum transfers is $n - S_{\max}$, where $n$ is the count of non-zero balances. Because determining whether even a single zero-sum subset exists is NP-complete (Subset Sum), the problem is NP-hard.

## Decision

Compute both greedy and exact algorithms in [`app/domain/simplify.py`](../../app/domain/simplify.py):

- **Two-heap greedy algorithm:** Always computed in $O(N \log N)$ using Python's `heapq`. Provides step-by-step audit logs showing the largest creditor and debtor at each step.
- **Exact bitmask dynamic programming (LeetCode 465):**
  - Runs in $O(2^n \cdot n)$ time.
  - Precomputes subset balance sums $s[\text{mask}]$.
  - Evaluates $dp[\text{mask}] = \max_{i \in \text{mask}} dp[\text{mask} \setminus \{i\}] + (1 \text{ if } s[\text{mask}] == 0 \text{ else } 0)$.
  - Traces back to identify the exact member groupings, then runs greedy within each isolated group to produce the optimal transfer edges.
- **The 15-member threshold:**
  - When non-zero balances $n \le 15$, the exact solver runs and SplitSync recommends `OPTIMAL`.
  - When $n > 15$, the exact solver is skipped, `optimal` returns `None`, and the plan returns `recommended = "GREEDY"`.

The spec's measurements (CPython, 2 vCPU dev container), by number of non-zero balances:
- 12 members: 3.7 ms
- 13 members: 12.8 ms
- 14 members: 22.2 ms
- 15 members: 35.6 ms
- 16 members: 75.4 ms

## Alternatives considered

- **Greedy heuristic only.** Fails to find the true minimum and leaves money transfers fragmented.
- **Mixed Integer Linear Programming (MILP) / Z3 SMT solver.** Introduces heavy non-Python dependencies, non-deterministic solver runtimes, and deployment overhead.
- **Branch and bound search.** High implementation complexity with unpredictable worst-case latency compared to bitmask DP.

## Consequences

- For standard groups ($n \le 15$), users receive the mathematically minimal set of settlement transfers.
- On the Goa demo room, debt reduction reaches 70% (10 pairwise IOUs $\rightarrow$ 3 optimal transfers, beating greedy's 4 transfers).
- For large groups ($n > 15$), the system remains fast and responsive, falling back smoothly to $O(N \log N)$ greedy transfers.
- Validated in [`tests/unit/test_simplify.py`](../../tests/unit/test_simplify.py).

## When we would revisit

If compiling the bitmask DP solver into Rust or C extensions via PyO3, which would allow the exact threshold to expand from 15 to 20 members while keeping latency under 50 ms.
