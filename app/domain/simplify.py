# app/domain/simplify.py
import heapq
import itertools

Edge = tuple[int, int, int]          # (from_member, to_member, amount_paise)


def pairwise_edges(member_ids, owed: dict[tuple[int, int], int]) -> list[Edge]:
    """IOUs as incurred, netted per pair. owed[(debtor, creditor)] comes from the SQL
    above (active expenses only). Never settlements."""
    edges = []
    for a, b in itertools.combinations(sorted(member_ids), 2):
        net = owed.get((a, b), 0) - owed.get((b, a), 0)
        if net > 0:
            edges.append((a, b, net))
        elif net < 0:
            edges.append((b, a, -net))
    return edges


def greedy(balances: dict[int, int]) -> tuple[list[Edge], list[dict]]:
    """Two-heap greedy: O(N log N), at most N-1 transfers, not always optimal."""
    creditors = [(-v, m) for m, v in balances.items() if v > 0]
    debtors = [(v, m) for m, v in balances.items() if v < 0]       # most negative first
    heapq.heapify(creditors)
    heapq.heapify(debtors)
    edges, steps = [], []
    while creditors and debtors:
        neg_cred, c = heapq.heappop(creditors)
        neg_debt, d = heapq.heappop(debtors)
        cred, debt = -neg_cred, -neg_debt
        amt = min(cred, debt)
        edges.append((d, c, amt))
        steps.append({"debtor": d, "debt_paise": debt, "creditor": c,
                      "credit_paise": cred, "amount_paise": amt})
        if cred > amt:
            heapq.heappush(creditors, (-(cred - amt), c))
        if debt > amt:
            heapq.heappush(debtors, (-(debt - amt), d))
    if creditors or debtors:
        raise ValueError("balances do not sum to zero")
    return edges, steps


def exact(balances: dict[int, int], max_n: int = 15):
    """Minimum transfers via bitmask DP (LeetCode 465): n - (most zero-sum groups).
    O(2^n * n). Returns (edges, groups), or None when more than max_n non-zero members."""
    ids = [m for m in sorted(balances) if balances[m] != 0]
    n = len(ids)
    if n == 0:
        return [], []
    if n > max_n:
        return None
    b = [balances[m] for m in ids]
    size = 1 << n
    s = [0] * size                                   # s[mask] = sum of balances in mask
    for mask in range(1, size):
        low = mask & -mask
        s[mask] = s[mask ^ low] + b[low.bit_length() - 1]
    dp = [0] * size                                  # dp[mask] = most zero-sum groups
    for mask in range(1, size):
        best, rest = 0, mask
        while rest:
            low = rest & -rest
            best = max(best, dp[mask ^ low])
            rest ^= low
        dp[mask] = best + (1 if s[mask] == 0 else 0)
    order, mask = [], size - 1                       # rebuild an order whose zero-sum
    while mask:                                      # prefixes are the groups
        need = dp[mask] - (1 if s[mask] == 0 else 0)
        rest = mask
        while rest:
            low = rest & -rest
            if dp[mask ^ low] == need:
                order.append(low.bit_length() - 1)
                mask ^= low
                break
            rest ^= low
    order.reverse()
    groups, current, running = [], [], 0
    for i in order:
        current.append(ids[i])
        running += b[i]
        if running == 0:
            groups.append(current)
            current = []
    edges = []
    for g in groups:                                 # greedy inside a group is optimal
        e, _ = greedy({m: balances[m] for m in g})
        edges.extend(e)
    assert len(edges) == n - dp[size - 1]
    return edges, groups


def plans(balances: dict[int, int], exact_max: int = 15) -> dict:
    """Greedy always; exact when at most exact_max members have a non-zero balance."""
    g_edges, g_steps = greedy(balances)
    ex = exact(balances, max_n=exact_max)
    return {
        "nonzero_members": sum(1 for v in balances.values() if v != 0),
        "greedy": {"count": len(g_edges), "edges": g_edges, "steps": g_steps},
        "optimal": None if ex is None else {"count": len(ex[0]), "edges": ex[0], "groups": ex[1]},
        "recommended": "GREEDY" if ex is None else "OPTIMAL",
    }
