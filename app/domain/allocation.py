# app/domain/allocation.py
def allocate(total: int, member_ids: list[int], weights: list[int] | None = None,
             rotation: int = 0) -> dict[int, int]:
    """Largest-remainder (Hamilton) split of `total` paise. EQUAL = all weights 1.
    Leftover paise go to the largest fractional remainders; ties are broken by the
    member order rotated by `rotation` (pass the expense id)."""
    if not member_ids:
        raise ValueError("no participants")
    if weights is None:
        weights = [1] * len(member_ids)
    if len(weights) != len(member_ids) or any(w <= 0 for w in weights):
        raise ValueError("one positive weight per member")
    w = dict(zip(member_ids, weights))
    ids = sorted(member_ids)
    total_w = sum(w.values())
    rot = rotation % len(ids)
    order = ids[rot:] + ids[:rot]
    pos = {m: i for i, m in enumerate(order)}
    base = {m: total * w[m] // total_w for m in ids}
    rem = {m: total * w[m] % total_w for m in ids}
    leftover = total - sum(base.values())
    winners = set(sorted(ids, key=lambda m: (-rem[m], pos[m]))[:leftover])
    out = {m: base[m] + (1 if m in winners else 0) for m in ids}
    assert sum(out.values()) == total
    return out
