# SplitSync — AI Build Spec

Oct 27, 2026 · @Srinu

## Instructions for the coding agent

Build SplitSync: a no-login, real-time group expense splitter with debt simplification, editable history and exactly-once settlement recording. Follow this spec literally; where it is silent, choose the simplest option and leave a `# DECISION:` comment.

**Non-negotiables (a build that breaks any of these is wrong):**

1. Money is integer paise end to end: `BIGINT` in SQL, `int` in Python. No `float` in any money path; parse input strings with `Decimal` (§4).
2. The ledger is append-only. No write path `UPDATE`s or `DELETE`s an expense, split, settlement or reversal row; edits and deletes add reversal rows (§5, §7). Only a demo-room reset and room expiry remove rows.
3. PostgreSQL guarantees correctness. Redis is used only for Pub/Sub, rate limits and a sweep leader lock, and every Redis call fails open (§7, §8).
4. Every write (member, expense add/edit/delete/restore, settlement record/delete, reset) first runs `SELECT … FROM groups WHERE slug = :slug FOR UPDATE`, then touches member rows in ascending id order.
5. Every expense and settlement write requires an `Idempotency-Key` header, checked *inside* the lock across all keyed tables and backed by `UNIQUE` indexes.
6. No `GET` changes state. Rooms are created only by `POST /api/rooms`. Naive mode and reset exist only in demo rooms.
7. The browser never renders user text with `innerHTML`; use `textContent` and `createElementNS`.
8. The seed in §11 must reproduce the listed numbers exactly, and every test in §12 must pass.

**Build order (commit after each phase, tests green):**

1. Pure domain modules — `money`, `allocation`, `simplify`, `ledger` — with unit and property tests.
2. Alembic migration for the schema in §3.
3. Add-expense, settlement, room and member writes (§7) with the integration and concurrency tests.
4. Edit, delete, restore and settlement delete (§7) with their race tests.
5. State snapshot, integrity checks, history feed and search (§5, §6, §9).
6. WebSockets and Redis Pub/Sub fan-out (§8).
7. Frontend (§10).
8. Docker, Compose, Nginx, CI/CD (§13), then security limits (§14).

## Stack and repository layout

Python 3.12, FastAPI on Uvicorn (2 workers), PostgreSQL 16 via SQLAlchemy 2.x async + asyncpg, Redis, vanilla JS. Versions below were current on PyPI on 27 Oct 2026; pin exact versions in a lock file (`uv lock` or `pip-tools`) and commit it.

| Component | Choice | Notes |
| --- | --- | --- |
| API | `fastapi` 0.162, `uvicorn[standard]` 0.54 | 2 workers, `--proxy-headers` |
| DB access | `sqlalchemy[asyncio]` 2.1, `asyncpg` 0.32, `alembic` 1.20 | Raw SQL via `text()` for the write paths |
| Validation | `pydantic` 2.14, `pydantic-settings` 2.16 | Strict models, `extra="forbid"` |
| Redis | `redis` 8.1 (`redis.asyncio`) | Pub/Sub, rate limits, sweep lock |
| Database | PostgreSQL 16 | Image `pgvector/pgvector:0.8.7-pg16` so it can be shared with TriageLens |
| Cache server | Redis 8 (`redis:8-alpine`) | `maxmemory 64mb`, `maxmemory-policy noeviction` |
| Frontend | HTML + vanilla JS + plain CSS | No build step, no CDN scripts |
| Tests | `pytest` 9.1, `pytest-asyncio` 1.4, `hypothesis` 6.170, `httpx` 0.28, `websockets` 17.3 | Integration tests need real Postgres and Redis |
| Lint | `ruff` 0.17 | `ruff check` + `ruff format --check` in CI |

```text
splitsync/
├── app/
│   ├── main.py              # app factory, lifespan: Redis listener + sweep task, routers, static files
│   ├── config.py            # Settings (pydantic-settings), read from env
│   ├── db.py                # async engine, session factory, write_tx() helper with retries
│   ├── redis_client.py      # pool, fail-open helpers, Lua scripts (release lock, rate limit)
│   ├── errors.py            # ApiError + RFC 9457 Problem Details handlers
│   ├── schemas.py           # Pydantic request/response models
│   ├── seed_data.py         # the Goa Road Trip members and 8 expenses (§11)
│   ├── domain/              # pure, synchronous, 100% unit-tested
│   │   ├── money.py         # parse_rupees()
│   │   ├── allocation.py    # allocate() largest remainder
│   │   ├── simplify.py      # pairwise_edges(), greedy(), exact()
│   │   └── ledger.py        # integrity report helpers
│   ├── services/
│   │   ├── rooms.py         # create demo/group, seed, reset, sweep
│   │   ├── members.py       # add member
│   │   ├── expenses.py      # add, edit, delete, restore
│   │   ├── settlements.py   # record (protected + naive), delete
│   │   ├── state.py         # snapshot builder, cached per (room, version)
│   │   ├── history.py       # activity feed, cursor-paged
│   │   ├── search.py        # expense search and filters, cursor-paged
│   │   └── realtime.py      # per-worker ConnectionManager + Redis Pub/Sub bridge
│   └── api/                 # thin routers: rooms, members, expenses, settlements, state, history, search, ws, health
├── migrations/              # Alembic, one revision per schema change
├── static/                  # index.html, room.html, app.js, graph.js, styles.css
├── tests/                   # unit/, integration/, e2e/
├── deploy/                  # nginx/, compose.prod.yml, setup_swap.sh, README-deploy.md
├── Dockerfile
├── docker-compose.yml       # local dev: app + postgres + redis
├── pyproject.toml, requirements.txt (pinned), requirements-dev.txt, .env.example, README.md
└── .github/workflows/       # ci-cd.yml: test → e2e → deploy
```

## Database schema

Ship this DDL as the first Alembic revision (`op.execute(...)`). It was run on PostgreSQL 16 and passed every check in §12, including concurrent edits and deletes and cascade-deleting a room that has edit history.

```sql
CREATE TABLE groups (                        -- one row per room
  id             BIGSERIAL PRIMARY KEY,
  slug           TEXT NOT NULL UNIQUE CHECK (slug ~ '^[A-Za-z0-9_-]{8,32}$'),
  kind           TEXT NOT NULL CHECK (kind IN ('DEMO','GROUP')),
  name           TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
  graph_version  BIGINT NOT NULL DEFAULT 1 CHECK (graph_version >= 1),   -- +1 per write, never reset
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL                                    -- set per kind by the service
);

CREATE TABLE members (
  id                BIGSERIAL PRIMARY KEY,
  group_id          BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version           BIGINT NOT NULL,                                     -- graph_version that added it
  name              TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
  color             TEXT NOT NULL CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
  net_balance_paise BIGINT NOT NULL DEFAULT 0,                           -- cache; the ledger is the truth
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (group_id, id)
);
CREATE UNIQUE INDEX members_group_name_ci ON members (group_id, lower(name));

CREATE TABLE expenses (                      -- every version of every expense; never updated
  id              BIGSERIAL PRIMARY KEY,
  group_id        BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version         BIGINT NOT NULL,                                       -- graph_version of the write
  lineage_id      BIGINT NOT NULL,                                       -- id of the first version
  revision_of     BIGINT UNIQUE,                                         -- previous version; one successor max
  paid_by         BIGINT NOT NULL,
  acted_by        BIGINT,                                                -- self-reported, may be NULL
  title           TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 80),
  title_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('simple', title)) STORED,
  spent_on        DATE NOT NULL,
  total_paise     BIGINT NOT NULL CHECK (total_paise BETWEEN 1 AND 100000000),
  split_type      TEXT NOT NULL CHECK (split_type IN ('EQUAL','SHARES','EXACT')),
  idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash    CHAR(64) NOT NULL,
  response_json   JSONB NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (group_id, idempotency_key),
  UNIQUE (group_id, id),
  CHECK ((revision_of IS NULL) = (lineage_id = id)),
  FOREIGN KEY (group_id, lineage_id)  REFERENCES expenses (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, revision_of) REFERENCES expenses (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, paid_by)     REFERENCES members (group_id, id)  DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, acted_by)    REFERENCES members (group_id, id)  DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE expense_splits (
  group_id    BIGINT NOT NULL,
  expense_id  BIGINT NOT NULL,
  member_id   BIGINT NOT NULL,
  weight      INTEGER CHECK (weight IS NULL OR weight BETWEEN 1 AND 100),   -- SHARES only
  share_paise BIGINT NOT NULL CHECK (share_paise >= 0),
  PRIMARY KEY (expense_id, member_id),
  FOREIGN KEY (group_id, expense_id) REFERENCES expenses (group_id, id) ON DELETE CASCADE,
  FOREIGN KEY (group_id, member_id)  REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE settlements (
  id                   BIGSERIAL PRIMARY KEY,
  group_id             BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version              BIGINT NOT NULL,
  from_member          BIGINT NOT NULL,
  to_member            BIGINT NOT NULL,
  acted_by             BIGINT,
  amount_paise         BIGINT NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
  mode                 TEXT NOT NULL CHECK (mode IN ('PROTECTED','NAIVE')),
  idempotency_key      TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash         CHAR(64) NOT NULL,
  graph_version_before BIGINT NOT NULL,
  response_json        JSONB NOT NULL,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (from_member <> to_member),
  UNIQUE (group_id, id),
  FOREIGN KEY (group_id, from_member) REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, to_member)   REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (group_id, acted_by)    REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE reversals (                     -- append-only: cancels one expense version or one settlement
  id              BIGSERIAL PRIMARY KEY,
  group_id        BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version         BIGINT NOT NULL,
  expense_id      BIGINT UNIQUE,                                         -- reversed at most once
  settlement_id   BIGINT UNIQUE,
  reason          TEXT NOT NULL CHECK (reason IN ('EDIT','DELETE')),
  acted_by        BIGINT,
  idempotency_key TEXT CHECK (char_length(idempotency_key) BETWEEN 16 AND 64),
  payload_hash    CHAR(64),
  response_json   JSONB,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(expense_id, settlement_id) = 1),
  CHECK (reason = 'DELETE' OR expense_id IS NOT NULL),                   -- settlements are only deleted
  CHECK ((reason = 'EDIT') = (idempotency_key IS NULL)),                 -- an edit's key lives on its new version
  CHECK (num_nulls(idempotency_key, payload_hash, response_json) IN (0, 3)),
  UNIQUE (group_id, idempotency_key),
  FOREIGN KEY (group_id, expense_id)    REFERENCES expenses (group_id, id)    ON DELETE CASCADE,
  FOREIGN KEY (group_id, settlement_id) REFERENCES settlements (group_id, id) ON DELETE CASCADE,
  FOREIGN KEY (group_id, acted_by)      REFERENCES members (group_id, id) DEFERRABLE INITIALLY DEFERRED
);

-- Every balance, graph and list query reads these two views, never the raw tables.
CREATE VIEW active_expenses AS
  SELECT e.* FROM expenses e
  WHERE NOT EXISTS (SELECT 1 FROM reversals r WHERE r.expense_id = e.id);

CREATE VIEW active_settlements AS
  SELECT s.* FROM settlements s
  WHERE NOT EXISTS (SELECT 1 FROM reversals r WHERE r.settlement_id = s.id);

-- Exactly-once backstop for the protected path only; NAIVE rows are allowed to duplicate.
CREATE UNIQUE INDEX settlements_idem_protected ON settlements (group_id, idempotency_key) WHERE mode = 'PROTECTED';
CREATE INDEX expenses_group_version    ON expenses (group_id, version DESC, id DESC);
CREATE INDEX expenses_group_spent      ON expenses (group_id, spent_on DESC, id DESC);
CREATE INDEX expenses_group_lineage    ON expenses (group_id, lineage_id);
CREATE INDEX expense_splits_group      ON expense_splits (group_id);
CREATE INDEX settlements_group_version ON settlements (group_id, version DESC, id DESC);
CREATE INDEX reversals_group_version   ON reversals (group_id, version DESC, id DESC);
CREATE INDEX groups_kind_expires       ON groups (kind, expires_at);
```

Why these constraints:

- **Every version is a row.** An edit never changes the old row: the old version gets a reversal row, and the new version is a new row whose `revision_of` points back. `lineage_id` (the first version's id) groups every version of one expense.
- **`UNIQUE (revision_of)` and the unique `expense_id` on reversals** make "at most one successor" and "reversed at most once" database facts. Two concurrent edits can never both apply, even with a bug in the service check.
- **`version`** on every ledger row is the `graph_version` of the write that created it. It orders the history feed (§9) and makes "the room as of version v" a simple filter.
- **Composite foreign keys** `(group_id, …) → (group_id, id)` make it impossible to point at a member, expense or settlement of another room.
- **`DEFERRABLE INITIALLY DEFERRED`** on member and self references is required: deleting a room cascades in an order that immediate checks would reject. Deferred, the checks run at commit and the room deletes cleanly (verified with edits and deletes present).
- **Idempotency backstops:** the partial unique index on protected settlements and `UNIQUE (group_id, idempotency_key)` on expenses and reversals. The service still looks the key up first, so it can replay the stored response.
- **Member names are unique ignoring case** (`lower(name)` index), so "Asha" and "asha" can't share a room.
- `expires_at` has no default: the service sets it per room kind (§14). Room caps are enforced in the service inside the room lock, not in SQL.

## Money and allocation

Amounts enter as decimal strings and become integer paise once, at the edge; shares come from a largest-remainder split that always sums exactly. Use these reference implementations as written — they passed 2,000 random property cases.

```python
# app/domain/money.py
import re
from decimal import Decimal

RUPEE_RE = re.compile(r"^(0|[1-9]\d{0,6})(\.\d{1,2})?$")
MAX_PAISE = 100_000_000          # ₹10,00,000.00


class InvalidAmount(ValueError):
    pass


def parse_rupees(s: str) -> int:
    """'100' -> 10000, '100.5' -> 10050, '0.01' -> 1. Never touches float."""
    s = s.strip()
    if not RUPEE_RE.match(s):
        raise InvalidAmount(s)
    paise = int((Decimal(s) * 100).to_integral_exact())
    if not 1 <= paise <= MAX_PAISE:
        raise InvalidAmount(s)
    return paise
```

| Input | Result |
| --- | --- |
| `"100"`, `"100.5"`, `"100.50"` | 10000, 10050, 10050 |
| `"0.01"`, `"999999.99"`, `"1000000"` | 1, 99999999, 100000000 |
| `"100.505"`, `"-5"`, `"1e3"`, `""`, `"abc"`, `"01"`, `"0"`, `"0.00"`, `"1000000.01"` | rejected → 422 `INVALID_AMOUNT` |

The frontend strips `₹`, commas and spaces before sending; the server accepts only the strict form.

```python
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
```

Expected outputs (member ids 3, 4, 5): `allocate(10000, [3, 4, 5])` → 3334 / 3333 / 3333; rotation 1 → 3333 / 3334 / 3333; rotation 2 → 3333 / 3333 / 3334. `allocate(240000, [P, R, K], [1, 1, 2])` → 60000 / 60000 / 120000.

Split rules:

- **EQUAL** — `allocate(total, ids, rotation=expense_id)`.
- **SHARES** — integer weights 1–100, `allocate(total, ids, weights, rotation=expense_id)`.
- **EXACT** — each listed share parsed with `parse_rupees`; Σ shares must equal the total exactly, else 422 `EXACT_SUM_MISMATCH` with `expected_paise` and `got_paise`.
- **Participants** — 1 up to the room's member cap (12 in a demo room, 50 in a group), distinct, all in the room; the payer need not be a participant.
- **`spent_on`** — optional ISO date; default today in Asia/Kolkata; allowed from 2000-01-01 to tomorrow. Search and the expense list sort by it.
- Get the expense id first with `SELECT nextval('expenses_id_seq')`, so the rotation and the stored `response_json` are known before the `INSERT`. An edit gets a new id, so an edited EQUAL split may move the extra paisa; a restore copies the old shares exactly.

## Ledger, cached balances and integrity

Expenses, splits, settlements and reversals are the ledger, and no write path changes or removes them. A row counts while no reversal points at it, which is exactly what the `active_expenses` and `active_settlements` views (§3) return. `members.net_balance_paise` is a cache, changed in the same transaction as the ledger rows; the integrity queries prove the two agree.

**Balance definition** (positive = owed money), over active rows only:

```latex
\text{net}(m) = \sum \text{paid}(m) - \sum \text{shares}(m) + \sum \text{settlements sent}(m) - \sum \text{settlements received}(m)
```

**Cache deltas per write** — merge them per member, then apply `SET net_balance_paise = net_balance_paise + :delta` in ascending member id, skipping zeros:

| Write | Deltas |
| --- | --- |
| Add expense | payer `+total`; each participant `−share` |
| Edit expense | the old version's deltas negated, plus the new version's |
| Delete expense | the version's deltas negated |
| Restore expense | the deleted version's deltas again |
| Record settlement D → C of A | D `+A`; C `−A` |
| Delete settlement | D `−A`; C `+A` |

Invariant after every committed write: Σ `net_balance_paise` over the room = 0.

**Integrity queries** (state builder). Cast every `sum` to `bigint`: Postgres returns `numeric` for `sum(bigint)`, which reaches Python as `Decimal` and breaks JSON encoding.

```sql
-- 1. cached vs ledger, per member
WITH paid AS (
  SELECT paid_by AS member_id, sum(total_paise)::bigint AS amt
  FROM active_expenses WHERE group_id = :gid GROUP BY paid_by
), owed AS (
  SELECT s.member_id, sum(s.share_paise)::bigint AS amt
  FROM expense_splits s JOIN active_expenses e ON e.id = s.expense_id
  WHERE s.group_id = :gid GROUP BY s.member_id
), sent AS (
  SELECT from_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY from_member
), recv AS (
  SELECT to_member AS member_id, sum(amount_paise)::bigint AS amt
  FROM active_settlements WHERE group_id = :gid GROUP BY to_member
)
SELECT m.id, m.net_balance_paise AS cached,
       COALESCE(paid.amt, 0) - COALESCE(owed.amt, 0) + COALESCE(sent.amt, 0) - COALESCE(recv.amt, 0) AS ledger
FROM members m
LEFT JOIN paid ON paid.member_id = m.id
LEFT JOIN owed ON owed.member_id = m.id
LEFT JOIN sent ON sent.member_id = m.id
LEFT JOIN recv ON recv.member_id = m.id
WHERE m.group_id = :gid
ORDER BY m.id;

-- 2. versions whose splits don't add up (must be 0)
SELECT count(*) FROM (
  SELECT e.id FROM expenses e LEFT JOIN expense_splits s ON s.expense_id = e.id
  WHERE e.group_id = :gid
  GROUP BY e.id, e.total_paise
  HAVING COALESCE(sum(s.share_paise), 0) <> e.total_paise) x;

-- 3. edit reversals without their new version (must be 0)
SELECT count(*) FROM reversals r
WHERE r.group_id = :gid AND r.reason = 'EDIT'
  AND NOT EXISTS (SELECT 1 FROM expenses n WHERE n.revision_of = r.expense_id AND n.version = r.version);
```

The snapshot exposes the result as:

```json
"invariants": {
  "zero_sum_paise": 0,
  "ledger_drift_total_paise": 0,
  "drifted_members": [],
  "bad_split_expenses": 0,
  "orphan_edits": 0
}
```

`drifted_members` lists `{member_id, cached_paise, ledger_paise, drift_paise}` for every non-zero drift, and `ledger_drift_total_paise` sums their absolute values. After the naive chaos run on a fresh demo room it must contain Aarav `+4500000` and Karthik `-4500000` while `zero_sum_paise` stays 0 (verified on this schema).

## Debt simplification algorithms

Four pure functions feed the two graphs: pairwise IOUs (left), and the greedy and exact plans (right). The code passed 3,000 random cases against a brute-force optimum; use it as written. The exact solver runs when at most 15 members have a non-zero balance; above that, the greedy plan is shown and labelled as greedy.

The left graph's raw totals come from SQL, so Python never loads every split:

```sql
-- owed[(debtor, creditor)]: the debtor's shares in active expenses the creditor paid
SELECT s.member_id AS debtor, e.paid_by AS creditor, sum(s.share_paise)::bigint AS amount
FROM expense_splits s JOIN active_expenses e ON e.id = s.expense_id
WHERE s.group_id = :gid AND s.member_id <> e.paid_by AND s.share_paise > 0
GROUP BY s.member_id, e.paid_by;
```

```python
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
```

Rules for using them:

- **Left graph** = `pairwise_edges(member_ids, owed)` over active expenses. Applying settlements pairwise is wrong: paying the optimal plan would leave 10 arrows that cancel per person. The snapshot sends the 200 largest edges plus the total count (`truncated: true` when cut).
- **Right graph** = `plans(cached_balances, EXACT_MAX_NONZERO)` with `EXACT_MAX_NONZERO = 15`. The UI shows `recommended` and lets people toggle to greedy.
- **Cost of exact**, measured in CPython 3.13 in the dev container (2 vCPUs), median of 5 runs: 3.7 ms at 12 non-zero members, 12.8 ms at 13, 22.2 ms at 14, 35.6 ms at 15, 75.4 ms at 16. Hence the switch at 15.
- Build plans once per graph version: cache them per `(group_id, graph_version)` in an in-process LRU of 256 entries, and compute them with `anyio.to_thread.run_sync` so the event loop isn't blocked.
- **Snapshot cost at the group cap**, measured on a room with 50 members, 9,900 expense versions (59,566 splits), 9,900 settlements and 1,000 reversals: median 154 ms per build — pairwise SQL 49 ms, integrity queries 100 ms, lists 2 ms, Python 1 ms. It runs once per version, after the write's response is sent.
- **Chaos target** (demo rooms only) = the largest transfer in the optimal plan, ties broken by lower `from` id, then lower `to` id. On a fresh seed that is Karthik → Aarav ₹5,000.
- Step log text for each greedy step: "Pop debtor {name} (owes ₹X) and creditor {name} (owed ₹Y) → {name} pays {name} ₹Z".

## Write paths and concurrency control

Every write is one READ COMMITTED transaction that locks its room row first, bumps `graph_version` once, and stamps every row it inserts with the new version. These sequences were run on PostgreSQL 16 with 10 concurrent connections; the results table at the end of this section comes from those runs.

### Shared helpers

- **`write_tx()`** — open with `engine.begin()`; first statements `SET LOCAL lock_timeout = '2s'` and `SET LOCAL statement_timeout = '5s'`. Retry the whole function on SQLSTATE `40001` or `40P01`, at most 3 attempts, sleeping a random 50–200 ms between them. Map `55P03` (lock timeout) to 409 `GROUP_BUSY` with `Retry-After: 1`. Never retry after a commit. After commit, build and publish the snapshot (§8) in a FastAPI background task, so the response doesn't wait for it.
- **`lock_room(slug)`** — `SELECT id, kind, graph_version FROM groups WHERE slug = :slug FOR UPDATE`; none → 404 `ROOM_NOT_FOUND`.
- **`lookup_idempotency(gid, key, payload_hash)`** — one query over every keyed table. Same hash → return the stored body with **200** and `Idempotent-Replayed: true`. Different hash → 422 `IDEMPOTENCY_KEY_REUSED`. Each operation's hash starts with its own name, so reusing a delete's key for an edit is caught too (verified).

```sql
SELECT payload_hash, response_json FROM expenses
 WHERE group_id = :gid AND idempotency_key = :key
UNION ALL
SELECT payload_hash, response_json FROM settlements
 WHERE group_id = :gid AND idempotency_key = :key AND mode = 'PROTECTED'
UNION ALL
SELECT payload_hash, response_json FROM reversals
 WHERE group_id = :gid AND idempotency_key = :key;
```

- **`bump(room)`** — `UPDATE groups SET graph_version = graph_version + 1, last_active_at = now(), expires_at = now() + :ttl WHERE id = :gid RETURNING graph_version`; `ttl` is 24 hours for a demo room and 365 days for a group.
- **Payload hashes** — `sha256` of the parts below joined with `|`; canonical body JSON = `json.dumps(body, sort_keys=True, separators=(",", ":"))` of the validated request body.

| Operation | Hashed parts |
| --- | --- |
| Add expense | `expense`, slug, canonical body |
| Edit expense | `edit`, slug, target expense id, canonical body |
| Delete expense | `delete-expense`, slug, expense id |
| Restore expense | `restore`, slug, expense id |
| Record settlement | `settle`, slug, from, to, amount\_paise (not `expected_graph_version`, so a retry from a refreshed screen still replays) |
| Delete settlement | `delete-settlement`, slug, settlement id |

- **`X-Acting-Member`** — optional header on every write: the member id the person picked as "I am". It must belong to the room (else 422 `MEMBER_NOT_IN_GROUP`), is stored in `acted_by`, and is not hashed. It is self-reported and the UI says so.

### Add expense — `POST /api/rooms/{slug}/expenses`

Expenses commute, so there is no version check: two different expenses at once must both succeed.

1. Validate and normalize the body (§4, §9); compute the payload hash.
2. `lock_room`, then `lookup_idempotency`.
3. Room already holds its cap of expense rows (every version counts) → 422 `ROOM_LIMIT` with `limit: "expenses"`. One `SELECT` confirms the payer, every participant and `acted_by` belong to the room → else 422 `MEMBER_NOT_IN_GROUP`.
4. `ver = bump(room)`; `eid = nextval('expenses_id_seq')`; shares from §4 with `rotation = eid`.
5. `INSERT` the expense with `id = eid`, `version = ver`, `lineage_id = eid`, `revision_of = NULL` and its `response_json`, then one row per split.
6. Apply the add deltas (§5); commit → **201** `{expense_id, lineage_id, graph_version, shares}`.

### Edit expense — `PUT /api/rooms/{slug}/expenses/{expense_id}`

The client sends the id of the version on its screen and the full new expense (same body as add). An edit fails only if this expense changed first, never because of an unrelated change elsewhere in the room.

1. Hash, `lock_room`, `lookup_idempotency`.
2. Load the target version and its splits; none → 404 `EXPENSE_NOT_FOUND`.
3. Target already reversed (edited or deleted) → 409 `EXPENSE_CHANGED` with `current_expense_id` (the lineage's live version, or `null`) and `deleted` (true when the lineage ends in a delete), from this query:

```sql
SELECT e.id, r.reason FROM expenses e LEFT JOIN reversals r ON r.expense_id = e.id
WHERE e.group_id = :gid AND e.lineage_id = :lineage
  AND NOT EXISTS (SELECT 1 FROM expenses s WHERE s.revision_of = e.id);
```

4. Same title, payer, amount, date, split type, participants and weights or amounts as the target → **200** `{"unchanged": true, "expense_id": …}` with no write.
5. Expense-row cap and membership checks, as in add.
6. `ver = bump(room)`; `eid = nextval(...)`; new shares with `rotation = eid`.
7. `INSERT INTO reversals (group_id, version, expense_id, reason, acted_by) VALUES (:gid, ver, :target, 'EDIT', :acted_by)` — no key on this row; the edit's key lives on the new version.
8. `INSERT` the new version with the target's `lineage_id`, `revision_of = target`, `version = ver`, then its splits.
9. Deltas = the old version negated plus the new version, merged per member; apply; commit → **200** `{expense_id, lineage_id, replaces, graph_version, shares}`.

### Delete expense — `DELETE /api/rooms/{slug}/expenses/{expense_id}`

1. Hash, `lock_room`, `lookup_idempotency`.
2. Load the target; none → 404; already reversed → 409 `EXPENSE_CHANGED`, as in edit.
3. `ver = bump(room)`; insert a `DELETE` reversal carrying the key, hash and response; apply the version's deltas negated; commit → **200** `{deleted_expense_id, lineage_id, graph_version}`.

### Restore expense — `POST /api/rooms/{slug}/expenses/{expense_id}/restore`

Undo for a delete. Only the deleted last version of a lineage can be restored.

1. Hash, `lock_room`, `lookup_idempotency`.
2. The target must have a `DELETE` reversal and no successor; otherwise 409 `EXPENSE_CHANGED`.
3. Expense-row cap check; `ver = bump(room)`; `eid = nextval(...)`.
4. `INSERT` a copy of the target as a new version (`revision_of = target`, same lineage) and copy its split rows and weights exactly — no re-allocation. Apply its deltas again; commit → **201** `{expense_id, lineage_id, restores, graph_version}`.

### Record settlement — `POST /api/rooms/{slug}/settlements` (protected)

Body `{"from_member": int, "to_member": int, "amount": "5000", "expected_graph_version": int, "mode": "PROTECTED"}`.

1. Validate: `from_member != to_member`, `parse_rupees(amount)`; hash.
2. `lock_room`, `lookup_idempotency`.
3. `graph_version != expected_graph_version` → 409 `STALE_GRAPH_VERSION` with `current_graph_version`.
4. `SELECT id, net_balance_paise FROM members WHERE group_id = :gid AND id = ANY(:ids) ORDER BY id FOR UPDATE`; fewer than 2 rows → 422 `MEMBER_NOT_IN_GROUP`.
5. `from` must owe (balance < 0), `to` must be owed (> 0), and `amount_paise <= min(-bal_from, bal_to)`; else 422 `OVER_SETTLEMENT` with `max_paise`. Room at its settlement cap → 422 `ROOM_LIMIT`.
6. Apply `from +A`, `to −A`; `ver = bump(room)`; `sid = nextval('settlements_id_seq')`.
7. `INSERT` the settlement with `version = ver`, `mode = 'PROTECTED'`, `graph_version_before`, and the receipt `{settlement_id, from_member, to_member, amount_paise, graph_version_before, graph_version_after}` as `response_json`; commit → **201** with the receipt.

### Record settlement — naive (demo rooms only)

Allowed only when `ENABLE_NAIVE_MODE=true` and the room is a demo room; otherwise 403 `NAIVE_MODE_DISABLED`. No key check, no version check, no business rule.

1. `SELECT id, kind, graph_version FROM groups WHERE slug = :slug` — no lock.
2. `SELECT id, net_balance_paise FROM members WHERE id = ANY(:ids)` — no lock.
3. `await asyncio.sleep(NAIVE_RACE_DELAY_MS / 1000)` (default 150) to widen the race window.
4. Ascending id: `UPDATE members SET net_balance_paise = :read_value ± amount WHERE id = :id` — the absolute write is the lost-update bug being demonstrated.
5. `INSERT` the settlement with `mode = 'NAIVE'` and `version = read_version + 1` (concurrent naive rows share it, which is part of the bug), then commit.
6. After the commit, as its own statement: `UPDATE groups SET graph_version = graph_version + 1 WHERE id = :gid` → 201. Bumping inside the transaction would hold member locks while waiting for the room row — the reverse of the protected lock order — and can deadlock against a protected writer.

### Delete settlement — `DELETE /api/rooms/{slug}/settlements/{settlement_id}`

1. Hash, `lock_room`, `lookup_idempotency`.
2. Load the settlement; none → 404 `SETTLEMENT_NOT_FOUND`; already reversed → 409 `SETTLEMENT_ALREADY_DELETED`.
3. `ver = bump(room)`; insert a `DELETE` reversal with `settlement_id`, key, hash and response; apply `from −A`, `to +A`; commit → **200** `{deleted_settlement_id, graph_version}`. Deleting a naive row works, but its drift stays — only a reset clears that.

### Rooms and members

- **Create room** `POST /api/rooms` with `{"kind": "DEMO"}` or `{"kind": "GROUP", "name": "Flat 4B", "members": ["Asha", "Ravi", …]}`. 503 `ROOMS_FULL` with `Retry-After: 600` when that kind's active-room cap is reached; 503 `STORAGE_FULL` when `pg_database_size(current_database())` exceeds `MAX_DB_SIZE_MB`. Then `slug = secrets.token_urlsafe(8)`; insert the room with `graph_version = 1` and `expires_at = now() + ttl`, and its members with `version = 1`. A group needs 2–50 names, unique ignoring case. A demo room then inserts the 8 seed expenses through the same internal add-expense function the endpoint uses, reaching version 9 → 201 `{slug, url, kind}`.
- **Add member** `POST /api/rooms/{slug}/members`: lock; at the member cap → 422 `ROOM_LIMIT`; `ver = bump`; insert with `version = ver` inside a savepoint; a unique violation on `lower(name)` → 409 `MEMBER_NAME_TAKEN`.
- **Reset** `POST /api/rooms/{slug}/reset` (demo rooms only, else 403 `RESET_NOT_ALLOWED`): lock; `ver = bump`; `DELETE` the room's settlements, expenses and members (splits and reversals cascade); re-insert the seed members with `version = ver` and the seed expenses. `graph_version` keeps rising — by 9 per reset — never reset it, or clients would ignore the new snapshots.
- **Expiry sweep**: every 10 min ± 60 s, each worker tries `SET splitsync:sweep <token> NX PX 120000`. The winner (or every worker, if Redis is down — the sweep is idempotent) runs `DELETE FROM groups WHERE id IN (SELECT id FROM groups WHERE expires_at < now() ORDER BY expires_at LIMIT 100)` until it deletes 0 rows, then releases the lock:

```lua
-- release only if we still own the lock
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
```

**Lock order everywhere:** room row first, then member rows in ascending id; the naive path touches only member rows (ascending) inside its transaction. This makes deadlocks between writers impossible; the 40P01 retry is a backstop.

**Verified race results** (10 requests at once, separate connections):

| Race | Result |
| --- | --- |
| 10 devices record the same settlement, distinct keys | 1 × 201, 9 × 409 `STALE_GRAPH_VERSION` |
| Same settlement request × 10, one key | 1 × 201, 9 × 200 replays, one receipt id |
| 10 different expenses | 10 × 201, version +10, drift 0 |
| 10 edits of one version, distinct keys | 1 × 200, 9 × 409 `EXPENSE_CHANGED` naming the new version |
| Same edit × 10, one key | 1 × 200, 9 × 200 replays, one new version |
| 5 deletes + 5 edits of one version | 1 success, 9 × 409 `EXPENSE_CHANGED` |
| 10 deletes of one settlement | 1 × 200, 9 × 409 `SETTLEMENT_ALREADY_DELETED` |
| Naive, 10 devices | 10 × 201, Σ 0, drift ±4500000 |

## Real-time sync: WebSockets and Redis Pub/Sub

Each worker holds only its own sockets, so every change is published to Redis and re-broadcast by every worker. Messages are full snapshots tagged with `graph_version`, so ordering and loss are handled by the client keeping the newest version.

### Endpoint `WS /ws/rooms/{slug}`

1. Check the `Origin` header against `ALLOWED_ORIGINS` (exact match). Not allowed → `close(code=1008)` before `accept()`. WebSockets are not covered by CORS, so this check is mandatory.
2. Room missing → accept, then close with code 4404.
3. Per worker: at most 20 sockets per room and 5 per client IP → close 1013 when exceeded.
4. Register the socket and send `{"type": "snapshot", "version": v, "state": {...}}` immediately.
5. Client sends `{"type": "ping"}` every 25 s; reply `{"type": "pong"}`. Ignore other messages; anything over 1 KB → close 1009.

### Per-worker `ConnectionManager`

- `rooms: dict[int, set[WebSocket]]`.
- `broadcast(group_id, text)`: send to each socket with `asyncio.wait_for(ws.send_text(text), timeout=2)` via `asyncio.gather`; drop and close any socket that errors or times out, so one slow phone can't stall the rest.

### Redis bridge

- **Publish** (writer, after commit): `await redis.publish(f"splitsync:room:{group_id}", snapshot_json)`. If publishing raises, call `manager.broadcast(...)` locally instead and log a warning, so at least this worker's sockets update.
- **Subscribe** (one task per worker, started in the FastAPI lifespan): `pubsub.psubscribe("splitsync:room:*")`, then for each `pmessage` parse the group id from the channel and call `manager.broadcast`. The writer's own worker receives its publish too, so never broadcast locally after a successful publish.
- On any Redis error the listener logs, sleeps 1 s doubling to 30 s, and re-subscribes. The API keeps serving.

### Client rules (`app.js`)

- Keep `currentVersion`; render a snapshot only if `msg.version > currentVersion`.
- On close, reconnect after 1, 2, 4, 8, 16, then every 30 s, each plus 0–500 ms jitter; the server sends a fresh snapshot on every connect.
- While the tab is visible, also `GET /api/rooms/{slug}/state` every 30 s and on `visibilitychange` → visible. This heals lost messages and covers a Redis outage.
- Show a status pill: **Live** (socket open), **Reconnecting**, or **Polling** (socket down, HTTP working).
- Nginx must set `proxy_read_timeout 75s`, longer than the 25 s ping, so idle sockets aren't cut.

## HTTP API and error format

Seventeen routes, all JSON except the two HTML pages. A write returns 201 when it creates a row and 200 otherwise; a replay returns 200 with `Idempotent-Replayed: true`. Every expense and settlement write needs `Idempotency-Key` (16–64 chars of `[A-Za-z0-9_-]`); every write accepts the optional `X-Acting-Member` (§7).

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| GET | `/` | Landing: Start demo, Create group, My groups | 200 HTML |
| GET | `/r/{slug}` | Room page; JS loads state and opens the socket | 200 HTML |
| POST | `/api/rooms` | Create a demo room or a group | 201 `{slug, url, kind}` |
| GET | `/api/rooms/{slug}/state` | Full snapshot; `ETag: "v{version}"`, `If-None-Match` → 304 | 200 |
| POST | `/api/rooms/{slug}/members` | Add a member `{name}` | 201 |
| POST | `/api/rooms/{slug}/expenses` | Add an expense | 201 |
| PUT | `/api/rooms/{slug}/expenses/{id}` | Edit: replace version `id` with a new version | 200 |
| DELETE | `/api/rooms/{slug}/expenses/{id}` | Delete version `id` | 200 |
| POST | `/api/rooms/{slug}/expenses/{id}/restore` | Undo a delete | 201 |
| GET | `/api/rooms/{slug}/expenses` | Search and filter, cursor-paged | 200 |
| POST | `/api/rooms/{slug}/settlements` | Record a settlement | 201 |
| DELETE | `/api/rooms/{slug}/settlements/{id}` | Delete a settlement | 200 |
| GET | `/api/rooms/{slug}/activity` | History feed, cursor-paged | 200 |
| POST | `/api/rooms/{slug}/reset` | Re-seed (demo rooms only) | 200 snapshot |
| GET | `/healthz` | Process is up | 200 |
| GET | `/readyz` | DB reachable; Redis `ok` or `degraded` | 200 / 503 |
| WS | `/ws/rooms/{slug}` | Live snapshots (§8) | — |

**Room bodies:** `{"kind": "DEMO"}` or `{"kind": "GROUP", "name": "Flat 4B", "members": ["Asha", "Ravi", "Meera"]}`.

**Expense bodies** (add and edit; `spent_on` optional):

```json
{"title": "Airport auto", "paid_by": 3, "amount": "100", "spent_on": "2026-10-27",
 "split": {"type": "EQUAL", "members": [{"id": 3}, {"id": 4}, {"id": 5}]}}

{"title": "Cafe brunch", "paid_by": 2, "amount": "2400",
 "split": {"type": "SHARES", "members": [{"id": 2, "weight": 1}, {"id": 3, "weight": 1}, {"id": 5, "weight": 2}]}}

{"title": "Club night", "paid_by": 3, "amount": "5000",
 "split": {"type": "EXACT", "members": [{"id": 1, "amount": "1200"}, {"id": 2, "amount": "900"},
           {"id": 3, "amount": "1000"}, {"id": 4, "amount": "800"}, {"id": 5, "amount": "1100"}]}}
```

**Settlement body:** `{"from_member": 5, "to_member": 1, "amount": "5000", "expected_graph_version": 9, "mode": "PROTECTED"}`.

**Snapshot** (`GET …/state`, WebSocket `state`, reset response):

```json
{
  "room": {"slug": "Qm9vS2V5MTIz", "kind": "DEMO", "name": "Goa Road Trip", "graph_version": 9,
           "expires_at": "2026-10-28T10:00:00Z"},
  "members": [{"id": 1, "name": "Aarav", "color": "#E4572E", "net_balance_paise": 500000}],
  "expenses": [{"id": 8, "lineage_id": 8, "title": "Club night", "paid_by": 3, "total_paise": 500000,
                "split_type": "EXACT", "spent_on": "2026-10-27", "revised": false, "acted_by": null,
                "shares": [{"member_id": 1, "share_paise": 120000}]}],
  "settlements": [{"id": 1, "from_member": 5, "to_member": 1, "amount_paise": 500000, "mode": "PROTECTED",
                   "acted_by": null, "created_at": "…"}],
  "graph": {
    "pairwise": {"count": 10, "truncated": false, "edges": [{"from": 4, "to": 1, "amount_paise": 288000}]},
    "nonzero_members": 5,
    "greedy": {"count": 4, "edges": [], "steps": []},
    "optimal": {"count": 3, "edges": [], "groups": [[4, 3, 2], [5, 1]]},
    "recommended": "OPTIMAL",
    "chaos_target": {"from": 5, "to": 1, "amount_paise": 500000}
  },
  "invariants": {"zero_sum_paise": 0, "ledger_drift_total_paise": 0, "drifted_members": [],
                 "bad_split_expenses": 0, "orphan_edits": 0},
  "limits": {"members": 12, "expense_rows": 200, "settlements": 500},
  "usage": {"members": 5, "expense_rows": 8, "settlements": 0}
}
```

`expenses` and `settlements` hold the newest 50 active rows by version. `revised` is true for a version that replaced or restored another. `optimal` is `null` and `recommended` is `GREEDY` when `nonzero_members` > 15. `chaos_target` appears only in demo rooms.

### History feed — `GET /api/rooms/{slug}/activity?limit=50&cursor=…&lineage=…`

Newest first; `limit` 1–100; `lineage` (an expense's lineage id) narrows the feed to that expense's own history. The cursor is the base64url of `version.rank.id` from the last item (rank: expense 3, settlement 2, reversal 1, member 0); a bad cursor → 400 `INVALID_CURSOR`. Run with `:lim` = limit + 1 to learn whether another page exists, and with `(:cv, :ck, :cid)` = `(2^63−1, 9, 2^63−1)` for the first page. Each branch is ordered and limited on its own, so the query reads at most 4 × (limit + 1) rows however long the history is (about 9 ms per page on the full-size test room). Paging through 21 items 4 at a time matched one big page exactly.

```sql
SELECT version, rank, id, kind, data FROM (
  (SELECT e.version, 3 AS rank, e.id, 'EXPENSE' AS kind,
          jsonb_build_object('title', e.title, 'total_paise', e.total_paise, 'paid_by', e.paid_by,
                             'lineage_id', e.lineage_id, 'revision_of', e.revision_of, 'parent_reason', pr.reason,
                             'old_title', p.title, 'old_total_paise', p.total_paise, 'spent_on', e.spent_on,
                             'acted_by', e.acted_by, 'at', e.created_at) AS data
   FROM expenses e
   LEFT JOIN expenses p ON p.id = e.revision_of
   LEFT JOIN reversals pr ON pr.expense_id = e.revision_of
   WHERE e.group_id = :gid AND (e.version, 3, e.id) < (:cv, :ck, :cid)
     AND (CAST(:lineage AS bigint) IS NULL OR e.lineage_id = :lineage)
   ORDER BY e.version DESC, e.id DESC LIMIT :lim)
  UNION ALL
  (SELECT s.version, 2, s.id, 'SETTLEMENT',
          jsonb_build_object('from', s.from_member, 'to', s.to_member, 'amount_paise', s.amount_paise,
                             'mode', s.mode, 'acted_by', s.acted_by, 'at', s.created_at)
   FROM settlements s
   WHERE s.group_id = :gid AND (s.version, 2, s.id) < (:cv, :ck, :cid) AND CAST(:lineage AS bigint) IS NULL
   ORDER BY s.version DESC, s.id DESC LIMIT :lim)
  UNION ALL
  (SELECT r.version, 1, r.id, 'REVERSAL',
          jsonb_build_object('expense_id', r.expense_id, 'settlement_id', r.settlement_id,
                             'title', x.title, 'total_paise', x.total_paise, 'lineage_id', x.lineage_id,
                             'from', st.from_member, 'to', st.to_member, 'amount_paise', st.amount_paise,
                             'acted_by', r.acted_by, 'at', r.created_at)
   FROM reversals r
   LEFT JOIN expenses x ON x.id = r.expense_id
   LEFT JOIN settlements st ON st.id = r.settlement_id
   WHERE r.group_id = :gid AND r.reason = 'DELETE' AND (r.version, 1, r.id) < (:cv, :ck, :cid)
     AND (CAST(:lineage AS bigint) IS NULL OR x.lineage_id = :lineage)
   ORDER BY r.version DESC, r.id DESC LIMIT :lim)
  UNION ALL
  (SELECT m.version, 0, m.id, 'MEMBER', jsonb_build_object('name', m.name, 'at', m.created_at)
   FROM members m
   WHERE m.group_id = :gid AND (m.version, 0, m.id) < (:cv, :ck, :cid) AND CAST(:lineage AS bigint) IS NULL
   ORDER BY m.version DESC, m.id DESC LIMIT :lim)
) a
ORDER BY version DESC, rank DESC, id DESC
LIMIT :lim;
```

Map each row to one item; `EDIT` reversals are skipped, because the new version that follows them is the "edited" item:

| Row | Item `type` | Extra fields |
| --- | --- | --- |
| EXPENSE, `revision_of` null | `EXPENSE_ADDED` | `expense_id, lineage_id, title, total_paise, paid_by, spent_on` |
| EXPENSE, parent reversed by EDIT | `EXPENSE_EDITED` | same + `replaces, old_title, old_total_paise` |
| EXPENSE, parent reversed by DELETE | `EXPENSE_RESTORED` | same + `restores` |
| REVERSAL of an expense | `EXPENSE_DELETED` | `expense_id, lineage_id, title, total_paise` |
| SETTLEMENT | `SETTLEMENT_RECORDED` | `settlement_id, from, to, amount_paise, mode` |
| REVERSAL of a settlement | `SETTLEMENT_DELETED` | `settlement_id, from, to, amount_paise` |
| MEMBER | `MEMBER_ADDED` | `member_id, name` |

Every item also carries `version`, `at` and `acted_by`. Response: `{"items": […], "next_cursor": "MTMuMS4y" | null}`.

### Search — `GET /api/rooms/{slug}/expenses`

Parameters (all optional): `q`, `paid_by`, `member` (took part), `min` and `max` (rupee strings, parsed with `parse_rupees`), `from` and `to` (ISO dates, inclusive, on `spent_on`), `include_deleted` (default false), `cursor`, `limit` (1–100, default 50). Results run newest `spent_on` first; the cursor is `{spent_on}_{id}` of the last row.

`q` is split in Python with `re.findall(r"\w+", q.lower())`, keeping at most 8 tokens of up to 40 characters. Each token becomes a prefix match, ANDed: `seafood din` finds "Beach-shack seafood dinner", and `vil` finds "Villa booking". Tokens go in as an array and are quoted in SQL, so characters such as `%_'&|!` are inert (verified). The `simple` text-search config is used on purpose: titles are short and full of names and places ("Dudhsagar", "Swiggy") that English stemming would only mangle.

```sql
WITH q AS (
  SELECT CASE WHEN cardinality(CAST(:tokens AS text[])) = 0 THEN NULL
              ELSE to_tsquery('simple', (SELECT string_agg(quote_literal(t) || ':*', ' & ')
                                         FROM unnest(CAST(:tokens AS text[])) AS t))
         END AS tsq
)
SELECT e.id, e.lineage_id, e.title, e.total_paise, e.paid_by, e.spent_on,
       (r.id IS NOT NULL) AS deleted, (e.revision_of IS NOT NULL) AS revised
FROM expenses e
LEFT JOIN reversals r ON r.expense_id = e.id
CROSS JOIN q
WHERE e.group_id = :gid
  AND (q.tsq IS NULL OR e.title_tsv @@ q.tsq)
  AND (r.id IS NULL OR (:include_deleted AND r.reason = 'DELETE'
                        AND NOT EXISTS (SELECT 1 FROM expenses n WHERE n.revision_of = e.id)))
  AND (CAST(:paid_by AS bigint) IS NULL OR e.paid_by = :paid_by)
  AND (CAST(:member AS bigint) IS NULL OR EXISTS (SELECT 1 FROM expense_splits s
                                                  WHERE s.expense_id = e.id AND s.member_id = :member))
  AND (CAST(:min AS bigint) IS NULL OR e.total_paise >= :min)
  AND (CAST(:max AS bigint) IS NULL OR e.total_paise <= :max)
  AND (CAST(:date_from AS date) IS NULL OR e.spent_on >= :date_from)
  AND (CAST(:date_to AS date) IS NULL OR e.spent_on <= :date_to)
  AND (e.spent_on, e.id) < (CAST(:cursor_date AS date), CAST(:cursor_id AS bigint))
ORDER BY e.spent_on DESC, e.id DESC
LIMIT :lim;
```

First page: `cursor_date = 9999-12-31`, `cursor_id = 2^63−1`. Only live versions match, plus — with `include_deleted` — the deleted last version of each lineage, so the UI can offer Restore. No GIN index: every search is scoped to one room, which the `(group_id, spent_on, id)` index narrows first; on the full-size test room, searches took 10–42 ms. Response items: `{expense_id, lineage_id, title, total_paise, paid_by, spent_on, deleted, revised}` plus `next_cursor`.

### Errors

RFC 9457 Problem Details (`application/problem+json`) with a stable `code`; convert FastAPI's `RequestValidationError` into the same shape.

```json
{"type": "/errors/expense-changed", "title": "Someone changed this expense first", "status": 409,
 "code": "EXPENSE_CHANGED", "detail": "Version 9 was replaced by version 10.",
 "current_expense_id": 10, "deleted": false}
```

| Code | Status | When |
| --- | --- | --- |
| `IDEMPOTENCY_KEY_MISSING` | 400 | Header absent or not 16–64 chars of `[A-Za-z0-9_-]` |
| `INVALID_CURSOR` | 400 | Cursor doesn't decode |
| `NAIVE_MODE_DISABLED` | 403 | `mode: NAIVE` in a group, or with `ENABLE_NAIVE_MODE=false` |
| `RESET_NOT_ALLOWED` | 403 | Reset on a group |
| `ROOM_NOT_FOUND` | 404 | Unknown or expired slug |
| `EXPENSE_NOT_FOUND` | 404 | No such expense version in this room |
| `SETTLEMENT_NOT_FOUND` | 404 | No such settlement in this room |
| `STALE_GRAPH_VERSION` | 409 | `expected_graph_version` ≠ current |
| `EXPENSE_CHANGED` | 409 | Target version already edited or deleted; body names the current version |
| `SETTLEMENT_ALREADY_DELETED` | 409 | Settlement already deleted |
| `GROUP_BUSY` | 409 | Lock wait over 2 s; `Retry-After: 1` |
| `MEMBER_NAME_TAKEN` | 409 | Duplicate member name, ignoring case |
| `VALIDATION_ERROR` | 422 | Body or query fails the schema |
| `INVALID_AMOUNT` | 422 | Amount string fails `parse_rupees` |
| `EXACT_SUM_MISMATCH` | 422 | Exact shares don't add up to the total |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same key, different request |
| `MEMBER_NOT_IN_GROUP` | 422 | A member id (or `X-Acting-Member`) from another room |
| `OVER_SETTLEMENT` | 422 | Amount exceeds what is owed |
| `ROOM_LIMIT` | 422 | A member, expense-row or settlement cap is reached; `limit` names it |
| `RATE_LIMITED` | 429 | Per-IP limit hit; `Retry-After` set |
| `ROOMS_FULL` | 503 | Active-room cap for that kind reached; `Retry-After: 600` |
| `STORAGE_FULL` | 503 | Database larger than `MAX_DB_SIZE_MB`; new rooms refused |

## Frontend

Two static pages, vanilla JS and plain CSS, no build step and no third-party scripts. It must work at 360 px wide and in dark mode.

**`index.html` (landing):**

- **Start demo** — `POST /api/rooms` `{"kind": "DEMO"}`, then go to the returned `url`.
- **Create a group** — group name plus member names, one per line (2–50); `POST /api/rooms` `{"kind": "GROUP", …}`.
- **My groups** — rooms this browser opened, kept in `localStorage` key `splitsync.rooms` as `[{slug, name, kind, last_opened}]`, newest first, at most 50. Wrap every storage read and write in `try/catch`; the page must work without storage.

**`room.html` (the app), top to bottom:**

1. **Status bar** — room name and a kind badge ("Demo · deleted after 24 h idle" or "Group · kept while in use"), graph version, Σ badge (green at ₹0.00), ledger-drift badge (green at ₹0, red listing each drifted member, e.g. "Aarav +₹45,000 · Karthik −₹45,000"), connection pill (Live / Reconnecting / Polling), **Copy link**, an **I am** member picker, and **Reset room** (demo rooms only).
2. **Graphs** — up to 15 members: left SVG "IOUs as incurred" with its count; right SVG "Plan" showing `recommended`, with an Optimal / Greedy toggle and a line such as "10 IOUs → 3 transfers (greedy 4)". A drawer shows greedy steps or optimal groups. Above 15 members: the left panel lists the 20 largest IOUs and the total count, and the right panel lists the plan's transfers. When `optimal` is null, label the plan "Greedy — more than 15 people have a balance, so the exact solver is skipped".
3. **Balances** — member and net amount, + in green, − in red, with the sign also written out.
4. **Expenses** — the newest 50 from the snapshot: title, date, amount, payer, an "edited" badge when `revised`, and **Edit · Delete · History** buttons per row. **Search** opens the search panel (item 8).
5. **Add / edit expense form** — title, payer, amount, date (default today), split type (Equal / Shares / Exact) with per-member checkboxes, weights or amounts. Edit opens the same form prefilled and sends `PUT` with the version's id. Plus a **"Add ₹100 split 3 ways"** shortcut in demo rooms (payer Rohan; Equal among Rohan, Sneha, Karthik).
6. **Record settlement** — from, to, amount (pre-filled from the plan), with `expected_graph_version` taken from the current snapshot. Settlement rows get **Delete** with a two-step inline confirm ("Delete" → "Confirm delete").
7. **History drawer** — the activity feed, newest first, **Load more** using `next_cursor`. One line per item, e.g. "Priya edited Fuel & tolls: ₹9,600 → ₹9,800 · 10:42", "Fuel & tolls deleted · Undo" (Undo shown only while that version is still the deleted last one). A row's **History** button opens the drawer filtered by its `lineage`. Names from `acted_by` carry a "self-reported" tooltip; show "Someone" when it is null.
8. **Search panel** — a text box (search 250 ms after typing stops; an `AbortController` cancels the previous request) and filters: paid by, involving, amount range, date range, show deleted. Results list with **Load more**; deleted results show **Restore**.
9. **Chaos panel** (demo rooms only) — Mode (Protected / Naive), Scenario ("10 devices" = 10 keys, or "Same request × 10" = 1 key), **Run**. It uses `graph.chaos_target`, fires all 10 `fetch`es in the same tick with `Promise.allSettled`, and logs each request's number, status, code, latency and receipt id, plus a summary like "1 × 201, 9 × 409".

**Graph drawing (`graph.js`):** members on a circle, each a filled circle in its color with initials and a name label. Edges are quadratic curves with arrowhead markers and an amount label on a small background rect at the midpoint. Pairwise edges are red and dashed; plan edges are green and solid, so color is never the only cue. Each SVG has `<title>` and `<desc>` plus a visually hidden table of its edges for screen readers.

**Behaviour rules:**

- Format money for display only: `new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR'}).format(paise / 100)` → ₹1,23,456.78. Dates: `new Intl.DateTimeFormat('en-IN', {day: 'numeric', month: 'short'})`.
- Create one `crypto.randomUUID()` key per user action and reuse it on automatic retries; disable the button while its request is in flight.
- **Delete** acts at once and shows a toast "Deleted 'Fuel & tolls' — Undo" for 10 s; Undo calls restore with a new key. Never use `confirm()` or `alert()`.
- The **I am** choice is stored per room in `localStorage` (`splitsync.me.{slug}`) and sent as `X-Acting-Member` on every write.
- On 409 `EXPENSE_CHANGED`: re-fetch state, say "Someone changed this expense first — showing the latest version", and reopen the editor on `current_expense_id` if there is one. On 409 `STALE_GRAPH_VERSION`: re-fetch and say "Someone just changed this room — check the plan and submit again." On 409 `GROUP_BUSY`: retry once after `Retry-After`.
- Show Problem Details `title` and `detail` in a toast inside an `aria-live="polite"` region.
- Build every element with `createElement` / `createElementNS` and set text with `textContent`. No `innerHTML`, no inline `onclick`, no inline `<script>`, so the CSP in §13 holds.
- Use the system font stack and CSS variables for colors, with a `prefers-color-scheme: dark` palette.

## Seed data (verified)

The seed was engineered so greedy is visibly suboptimal: 10 pairwise IOUs, 4 greedy transfers, 3 optimal. Every expected value below was produced by running §4, §6 and §7 on PostgreSQL 16; tests must assert them exactly.

```python
# app/seed_data.py — insert members in this order; amounts in paise
ROOM_NAME = "Goa Road Trip"
MEMBERS = [("Aarav", "#E4572E"), ("Priya", "#17BEBB"), ("Rohan", "#FFC914"),
           ("Sneha", "#76B041"), ("Karthik", "#7E52A0")]
ALL = ["Aarav", "Priya", "Rohan", "Sneha", "Karthik"]
EXPENSES = [
    # (title, payer, total_paise, split_type, participants, weights_or_exact, days_ago)
    ("Villa booking (2 nights)",   "Aarav",   1_020_000, "EQUAL",  ALL, None, 3),
    ("Fuel & tolls",               "Priya",     960_000, "EQUAL",  ALL, None, 3),
    ("Beach-shack seafood dinner", "Rohan",     610_000, "EQUAL",  ALL, None, 2),
    ("Scooter rentals",            "Sneha",     330_000, "EQUAL",  ALL, None, 2),
    ("Dudhsagar jeep safari",      "Karthik",   580_000, "EQUAL",  ALL, None, 1),
    ("Parasailing",                "Aarav",     450_000, "EQUAL",  ["Aarav", "Sneha", "Karthik"], None, 1),
    ("Cafe brunch",                "Priya",     240_000, "SHARES", ["Priya", "Rohan", "Karthik"], [1, 1, 2], 0),
    ("Club night",                 "Rohan",     500_000, "EXACT",  ALL,
        {"Aarav": 120_000, "Priya": 90_000, "Rohan": 100_000, "Sneha": 80_000, "Karthik": 110_000}, 0),
]
# spent_on = today in Asia/Kolkata minus days_ago
```

All seed totals divide evenly, so rotation never changes a seed share.

**Expected right after `POST /api/rooms`:**

| Value | Expected |
| --- | --- |
| `graph_version` | 9 (starts at 1, +1 per seed expense) |
| Balances (paise) | Aarav 500000 · Priya 350000 · Rohan 250000 · Sneha -600000 · Karthik -500000 |
| Invariants | `zero_sum_paise` 0 · `ledger_drift_total_paise` 0 · `bad_split_expenses` 0 |
| Pairwise (10) | Sneha→Aarav 288000 · Karthik→Aarav 238000 · Karthik→Priya 196000 · Sneha→Rohan 136000 · Sneha→Priya 126000 · Karthik→Rohan 116000 · Sneha→Karthik 50000 · Rohan→Priya 40000 · Aarav→Rohan 38000 · Priya→Aarav 12000 |
| Greedy (4, in order) | Sneha→Aarav 500000 · Karthik→Priya 350000 · Karthik→Rohan 150000 · Sneha→Rohan 100000 |
| Optimal (3) | Sneha→Priya 350000 · Sneha→Rohan 250000 · Karthik→Aarav 500000 |
| Optimal groups | \[Sneha, Rohan, Priya\] and \[Karthik, Aarav\] |
| `chaos_target` | Karthik → Aarav 500000 |

**Expected after the demo actions:**

| Action | Expected |
| --- | --- |
| Protected, 10 distinct keys, Karthik→Aarav 500000 at v9 | exactly 1 × 201 and 9 × 409 `STALE_GRAPH_VERSION`; version 10; 1 settlement row; drift 0 |
| Then protected, one key × 10, new target Sneha→Priya 350000 | exactly 1 × 201 and 9 × 200 replays with the same `settlement_id`; version 11; drift 0 |
| Same key again with amount 100000 | 422 `IDEMPOTENCY_KEY_REUSED` |
| Naive, fresh room, 10 distinct keys, Karthik→Aarav 500000 | 10 × 201; 10 rows; Σ cached 0; drift Aarav +4500000, Karthik -4500000; cached Aarav 0, Karthik 0 |
| 10 concurrent different expenses, fresh room | 10 × 201; version 9 → 19; drift 0 |
| "Add ₹100 split 3 ways" | shares {3334, 3333, 3333}; which member gets 3334 depends on `expense_id % 3` |

**Expected for edits and deletes** (fresh demo room, steps in this order, from the verification run):

| Action | Expected |
| --- | --- |
| Edit "Fuel & tolls" ₹9,600 → ₹9,800, Equal among all 5 | 200; version 10; balances Aarav 496000 · Priya 366000 · Rohan 246000 · Sneha -604000 · Karthik -504000; pairwise 10, greedy 4, optimal 4 |
| Send the same body to the new version | 200 `{"unchanged": true}`; version stays 10 |
| Edit the old, replaced version | 409 `EXPENSE_CHANGED`, `current_expense_id` = the new version |
| 10 concurrent edits of the current version, distinct keys | 1 × 200, 9 × 409 `EXPENSE_CHANGED`; version +1; drift 0 |
| One edit request × 10, same key | 1 × 200, 9 × 200 replays; one new version |
| Delete the current ₹9,800 Fuel version | 200; balances Aarav 692000 · Priya -418000 · Rohan 442000 · Sneha -408000 · Karthik -308000 |
| Delete again: same key / new key | 200 replay / 409 `EXPENSE_CHANGED` with `deleted: true` |
| Restore it | 201; balances back to the ₹9,800 values |
| Delete and restore "Cafe brunch" | splits and weights identical: 60000 / 60000 / 120000 with weights 1 / 1 / 2 |
| Record a plan transfer, then 10 concurrent deletes of it | 1 × 200, 9 × 409 `SETTLEMENT_ALREADY_DELETED`; balances back to before the settlement |
| Reuse a delete's key for an edit | 422 `IDEMPOTENCY_KEY_REUSED` |
| Reset after all of the above | seed balances and invariants; `graph_version` +9 |

## Tests and acceptance criteria

Three layers; all must pass in CI before any deploy. Concurrency tests must use separate DB connections (pool size ≥ 12) and fire requests with `asyncio.gather`, never in a loop.

**Unit (`tests/unit`, no services):**

- `test_money.py` — every accept/reject row in §4.
- `test_allocation.py` — the §4 examples; Hypothesis: for random totals (1–10⁸), 1–50 members, weights 1–100 and any rotation, Σ shares == total and every share is within 1 paisa of total × w ÷ Σw.
- `test_simplify.py` — the seed values in §11 exactly; counter-example {+4, +3, +3, −6, −4} → greedy 4, exact 3; Hypothesis on random zero-sum balances (n ≤ 7): both plans settle every balance exactly, exact ≤ greedy ≤ n−1, and exact equals a brute-force partition search; `plans()` with 16 non-zero balances returns `optimal: None` and `recommended: "GREEDY"`.

**Integration (`tests/integration`, real Postgres 16 + Redis):**

- `test_rooms.py` — demo room → every "right after" row of §11; group with 50 names → 201, a 51st member → 422 `ROOM_LIMIT`; names that differ only in case → 422; unknown slug → 404; with MAX\_ACTIVE\_DEMO\_ROOMS=2 the third demo room → 503.
- `test_expenses.py` — replay returns 200 + `Idempotent-Replayed: true` + identical body; changed body → 422; missing key → 400; EXACT mismatch → 422 with both totals; foreign member or foreign `X-Acting-Member` → 422; 10 concurrent different expenses → §11 row.
- `test_edit_delete.py` — every row of §11 "Expected for edits and deletes", in order, including both concurrency rows and the key-reuse row.
- `test_settlements.py` — every "after the demo" row of §11, `OVER_SETTLEMENT`, `NAIVE_MODE_DISABLED` in a group, and the concurrent settlement-delete row.
- `test_history.py` — after the edit/delete sequence, the feed holds `EXPENSE_ADDED` × 8, `MEMBER_ADDED` × 5 and the right edited/deleted/restored items; paging 4 at a time returns exactly the same items as one page of 100; `lineage` returns only that expense's items; a garbage cursor → 400.
- `test_search.py` — `vil` → Villa; `seafood din` → the dinner; a deleted expense is hidden, then shown with `include_deleted`; paid-by, member, amount and date filters; paging 3 at a time equals one big page; `q="%_'&|!"` → 200 with no error.
- `test_invariants.py` — after every test above: `zero_sum_paise` 0, drift 0, `bad_split_expenses` 0, `orphan_edits` 0.
- `test_reset.py` — reset after edits, deletes and chaos restores the seed balances, `graph_version` keeps rising; reset in a group → 403 `RESET_NOT_ALLOWED`; deleting a room with history cascades to 0 rows.
- `test_ws.py` — snapshot on connect; snapshot with a higher version after a write; wrong `Origin` rejected with 1008.
- `test_redis_down.py` — point the app at an unused Redis port: writes still succeed with correct results; `/readyz` returns 200 with `"redis": "degraded"`.

**End-to-end (`tests/e2e`, Docker Compose with `--workers 2`):**

- `test_multiworker_fanout.py` — open WebSocket clients until they cover at least 2 distinct worker PIDs (with EXPOSE\_WORKER\_PID=true the first snapshot carries `worker_pid`; never enable it in production), `POST` one expense, and assert every open socket on every worker receives a snapshot with the new version within 2 s.

**Load check (`tests/load/big_room.py`, run manually, not in CI):** fill a group to its caps — 50 members, 9,900 expense versions, 9,900 settlements, 1,000 reversals — with `COPY`, then time the snapshot build, a history page, five searches and 20 writes. Reference medians from the dev container (2 vCPUs): snapshot 154 ms, history page 9 ms, searches 10–42 ms, write 22.5 ms including a new connection. Record your EC2 numbers in the README.

**CI services:** run `pgvector/pgvector:0.8.7-pg16` and `redis:8-alpine` as service containers with health checks, apply migrations with `alembic upgrade head`, then run unit → integration. Run the e2e job after building the image.

**Acceptance:** `ruff check` clean, all tests green three runs in a row (to catch flaky timing), and a manual pass of the demo script in the Master Guide on the deployed site.

## Deployment: Docker, Nginx, CI/CD, EC2

One EC2 host runs both portfolio apps behind one Nginx, one Postgres (two databases) and one Redis. Images are built only in GitHub Actions; the host only pulls, because building on a 1 GB machine risks the OOM killer.

### Dockerfile

```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY static ./static
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
# The app port is never published on the host; only Nginx on the Docker network reaches it,
# which is why trusting forwarded headers from any address is acceptable here.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

### Production Compose (`/opt/portfolio/compose.prod.yml`, shared with TriageLens)

```yaml
services:
  nginx:
    image: nginx:stable-alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - ./certbot-www:/var/www/certbot:ro
    depends_on: [splitsync]
    restart: unless-stopped
  db:
    image: pgvector/pgvector:0.8.7-pg16
    env_file: ./db.env                       # POSTGRES_PASSWORD
    command: ["postgres", "-c", "shared_buffers=128MB", "-c", "max_connections=60",
              "-c", "work_mem=4MB", "-c", "maintenance_work_mem=64MB"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./initdb:/docker-entrypoint-initdb.d:ro   # creates the splitsync + triagelens DBs and roles
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"], interval: 5s, retries: 10}
    restart: unless-stopped
  redis:
    image: redis:8-alpine
    command: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "noeviction",
              "--save", "", "--appendonly", "no"]
    restart: unless-stopped
  splitsync:
    image: ghcr.io/${GH_OWNER}/splitsync:${SPLITSYNC_TAG}
    env_file: ./splitsync.env                # DATABASE_URL, REDIS_URL, ALLOWED_ORIGINS, ...
    depends_on: {db: {condition: service_healthy}, redis: {condition: service_started}}
    restart: unless-stopped
volumes:
  pgdata:
```

SQLAlchemy pool per worker: `pool_size=5, max_overflow=5`, so SplitSync uses at most 20 of the 60 Postgres connections. After the first deploy, run `docker stats` and add `mem_limit` values from what you measure; if the containers plus 2 GB swap still struggle, move to a 2 GB instance.

### Nginx (`nginx/conf.d/splitsync.conf`)

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
limit_req_zone $binary_remote_addr zone=splitsync_api:10m rate=10r/s;

server {
  listen 80;
  server_name splitsync.YOURDOMAIN;
  location /.well-known/acme-challenge/ { root /var/www/certbot; }
  location / { return 301 https://$host$request_uri; }
}

server {
  listen 443 ssl;
  http2 on;                                   # nginx >= 1.25.1 syntax
  server_name splitsync.YOURDOMAIN;
  ssl_certificate     /etc/letsencrypt/live/splitsync.YOURDOMAIN/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/splitsync.YOURDOMAIN/privkey.pem;

  add_header Strict-Transport-Security "max-age=31536000" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "same-origin" always;
  add_header Content-Security-Policy "default-src 'self'; connect-src 'self' wss://splitsync.YOURDOMAIN; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'" always;

  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;

  location /ws/ {
    proxy_pass http://splitsync:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # a location that sets any header
    proxy_set_header X-Forwarded-Proto $scheme;                     # inherits none from the server block
    proxy_read_timeout 75s;
  }
  location /api/ {
    limit_req zone=splitsync_api burst=30 nodelay;   # the 10-request chaos burst fits
    proxy_pass http://splitsync:8000;
  }
  location / { proxy_pass http://splitsync:8000; }
}
```

HTTP/2 matters for the demo: over HTTP/1.1 Chrome opens at most 6 connections per host, so 4 of the 10 chaos requests would queue.

### One-time EC2 setup

1. Launch Ubuntu 24.04 LTS with a 20 GB gp3 disk; attach an Elastic IP. Security group: 80 and 443 from anywhere, 22 from your IP only. Public IPv4 is billed hourly, so check the billing page.
2. Point `splitsync.YOURDOMAIN` and `triagelens.YOURDOMAIN` A records at the Elastic IP.
3. Install Docker Engine and the Compose plugin from Docker's official Ubuntu instructions; disable SSH password login.
4. Run `deploy/setup_swap.sh` (below).
5. Copy `compose.prod.yml`, `nginx/`, `initdb/` and the env files to `/opt/portfolio`; `docker login ghcr.io` with a read-only packages token (or make the images public).
6. Start Nginx with only the port-80 blocks, run `certbot/certbot` in webroot mode for both names, then enable the 443 blocks and `docker compose -f compose.prod.yml up -d`. Add a weekly `certbot renew` + `nginx -s reload` cron job.

```bash
#!/usr/bin/env bash
# deploy/setup_swap.sh — 2 GB swap for a 1 GB instance
set -euo pipefail
if swapon --show | grep -q /swapfile; then echo "swap already enabled"; exit 0; fi
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
```

### CI/CD (`.github/workflows/ci-cd.yml`)

```yaml
name: ci-cd
on: {push: {branches: [main]}, pull_request: {}}
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:0.8.7-pg16
        env: {POSTGRES_PASSWORD: test, POSTGRES_DB: splitsync_test}
        ports: ["5432:5432"]
        options: --health-cmd "pg_isready -U postgres" --health-interval 5s --health-retries 10
      redis:
        image: redis:8-alpine
        ports: ["6379:6379"]
        options: --health-cmd "redis-cli ping" --health-interval 5s --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://postgres:test@localhost:5432/splitsync_test
      REDIS_URL: redis://localhost:6379/0
      ENABLE_NAIVE_MODE: "true"
      RATE_LIMIT_ENABLED: "false"
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: {python-version: "3.12"}
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: ruff check . && ruff format --check .
      - run: alembic upgrade head
      - run: pytest tests/unit tests/integration -q
  e2e:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: {python-version: "3.12"}
      - run: docker compose up -d --build --wait
      - run: pip install -r requirements-dev.txt && pytest tests/e2e -q
  deploy:
    needs: [test, e2e]
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: {contents: read, packages: write}
    steps:
      - uses: actions/checkout@v7
      - uses: docker/setup-buildx-action@v4
      - uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v7
        with:
          context: .
          push: true
          platforms: linux/amd64        # t4g (ARM) host: add linux/arm64 and docker/setup-qemu-action@v4
          tags: |
            ghcr.io/${{ github.repository_owner }}/splitsync:${{ github.sha }}
            ghcr.io/${{ github.repository_owner }}/splitsync:latest
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.EC2_HOST }}
          username: ${{ secrets.EC2_USER }}
          key: ${{ secrets.EC2_SSH_KEY }}
          script: |
            set -e
            cd /opt/portfolio
            PREV=$(grep '^SPLITSYNC_TAG=' .env | cut -d= -f2)
            sed -i "s/^SPLITSYNC_TAG=.*/SPLITSYNC_TAG=${{ github.sha }}/" .env
            docker compose -f compose.prod.yml pull splitsync
            docker compose -f compose.prod.yml run --rm splitsync alembic upgrade head
            docker compose -f compose.prod.yml up -d splitsync
            sleep 5
            if ! curl -fsS https://splitsync.YOURDOMAIN/readyz; then
              sed -i "s/^SPLITSYNC_TAG=.*/SPLITSYNC_TAG=$PREV/" .env
              docker compose -f compose.prod.yml up -d splitsync
              exit 1
            fi
```

The rollback re-deploys the previous image but cannot undo a migration, so every migration must be backward-compatible (add columns first, drop them in a later release). Repository secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`.

## Security, limits and observability

The site is public and has no login, so every resource is capped, every room is private by URL, every user string is treated as hostile, and every change stays visible and reversible in the history.

**Configuration (environment variables, read by `config.py`):**

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | — | `postgresql+asyncpg://…` |
| `REDIS_URL` | — | `redis://redis:6379/0` |
| `ALLOWED_ORIGINS` | `http://localhost:8000` | Comma list checked on every WebSocket handshake |
| `ENABLE_NAIVE_MODE` | `true` | Allows `mode: NAIVE` in demo rooms |
| `NAIVE_RACE_DELAY_MS` | `150` | Read-to-write gap in the naive path |
| `DEMO_LIMITS` | `12,200,500` | Members, expense rows (all versions), settlements per demo room |
| `GROUP_LIMITS` | `50,10000,10000` | The same caps for a group |
| `DEMO_TTL_HOURS` | `24` | A demo room is deleted after this long idle |
| `GROUP_TTL_DAYS` | `365` | A group is deleted after this long idle |
| `MAX_ACTIVE_DEMO_ROOMS` | `500` | Demo-room creation returns 503 above this |
| `MAX_ACTIVE_GROUPS` | `1000` | Group creation returns 503 above this |
| `MAX_DB_SIZE_MB` | `4096` | New rooms refused with 503 `STORAGE_FULL` above this database size |
| `EXACT_MAX_NONZERO` | `15` | Most non-zero balances the exact solver will handle |
| `RATE_LIMIT_ENABLED` | `true` | `false` in CI test and e2e environments |
| `LOG_LEVEL` | `INFO` | JSON logs to stdout |

The group caps come from the load test in §12: at full size a room's snapshot took a median 154 ms and the whole test database was 17.6 MB. That makes the database-size guard, not the room count, what protects a 20 GB disk.

**Per-IP rate limits** (Redis fixed window; `429 RATE_LIMITED` with `Retry-After`; skipped if Redis is down):

| Action | Limit |
| --- | --- |
| Create room | 10 per hour |
| Any write (`POST`, `PUT`, `DELETE`) | 60 per minute |
| Requests carrying `mode: NAIVE` | 30 per minute (3 chaos runs) |
| Search and history reads | 120 per minute |
| WebSocket connects | 30 per minute |

```lua
-- rate_limit.lua  KEYS[1] = rl:{scope}:{ip}:{window_start}, ARGV[1] = window ms; returns the count
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return n
```

The client IP comes from `X-Forwarded-For` as rewritten by Uvicorn's `--proxy-headers`; the app port is reachable only from Nginx.

Add `RATE_LIMIT_ENABLED` (default `true`) and set it to `false` in the CI test and e2e environments, or the concurrency tests will trip the per-minute write limit; one dedicated test turns it on and asserts the 429.

**Hard rules:**

- Room slugs come from `secrets.token_urlsafe(8)` (11 characters, about 64 bits). Never expose numeric room ids in URLs.
- No CORS middleware: the API is same-origin only.
- Every user string (names, titles) reaches the DOM through `textContent`; the CSP in §13 blocks inline script as a second layer.
- Pydantic models use `extra="forbid"`, `str` fields have `max_length`, and the request body limit is 16 KB.
- No secrets in the repo; `.env.example` lists names only.

**Trust model:** anyone with a room's link can add, edit or delete — the usual trade-off of a no-login app, softened by unguessable links. Reversal rows keep every change in the history and undoable, and `acted_by` is always shown as self-reported.

**Observability:**

- A middleware assigns `X-Request-ID` (or keeps a valid incoming one) and logs one JSON line per request: `request_id, method, path, status, latency_ms, room_slug, outcome`.
- `outcome` is one of `CREATED, REPLAYED, STALE, BUSY, REJECTED, RATE_LIMITED`, which makes the chaos runs easy to read in `docker compose logs`.
- Never log request bodies.
- `/healthz` returns 200 if the process is up. `/readyz` returns 200 when `SELECT 1` succeeds, with `{"db": "ok", "redis": "ok" | "degraded"}`; it returns 503 only when the database is unreachable.

## Definition of done

Ship when every box is ticked; each maps to a section above.

- [ ] No `float` in any money path; `grep -rn "float" app/domain app/services` shows nothing money-related (§4).
- [ ] No write path issues `UPDATE` or `DELETE` on expenses, splits, settlements or reversals, except reset and expiry (§7).
- [ ] Seed reproduces §11 exactly: 10 pairwise, 4 greedy, 3 optimal, version 9, Σ 0, drift 0.
- [ ] Every row of §11 "Expected for edits and deletes" passes, including 1 × 200 + 9 × 409 for 10 concurrent edits.
- [ ] Protected chaos: 1 × 201 + 9 × 409; rage-click: 1 × 201 + 9 × 200 with one receipt id; naive: ±₹45,000 drift (§11).
- [ ] History pages match one big page; search finds prefixes, hides deleted rows unless asked, and survives special characters (§9).
- [ ] A group of 50 members works; above 15 non-zero balances the plan is greedy and labelled so (§6, §10).
- [ ] Unit, integration and e2e tests pass three runs in a row in CI (§12).
- [ ] A write on one worker updates sockets on the other worker (e2e fan-out test) (§8).
- [ ] With Redis stopped, writes still succeed and `/readyz` reports `degraded` (§12).
- [ ] Wrong `Origin` on the WebSocket is rejected; no `innerHTML` with user data anywhere (§8, §10).
- [ ] Rate limits return 429 with `Retry-After`; demo rooms expire after 24 h idle, groups after 365 days (§14, §7).
- [ ] Site is live over HTTPS with HTTP/2; the CSP header is present; the chaos burst runs concurrently (§13).
- [ ] `git push` to `main` runs tests, builds, pushes to GHCR, deploys, migrates, health-checks and rolls back on failure (§13).
- [ ] README covers local setup (`docker compose up`), the test commands, the architecture diagram, your EC2 load-check numbers and a link to the live demo.

## Sources

- [IETF draft: The Idempotency-Key HTTP Header Field (-07)](https://www.ietf.org/archive/id/draft-ietf-httpapi-idempotency-key-header-07.html) — 400 / 409 / 422 semantics.
- [nginx 1.25.1 announcement](https://mailman.nginx.org/pipermail/nginx-announce/2023/BYSVLPUZESCZHJMTDD25QD7ZKZYADAR2.html) — the `http2` directive.
- [pgvector Docker Hub tags](https://hub.docker.com/v2/repositories/pgvector/pgvector/tags?page_size=25&name=pg16) — `0.8.7-pg16`.
- Package versions: PyPI, checked 27 Oct 2026. All seed numbers, algorithm outputs and concurrency results: reproduced by running this spec's code and SQL on PostgreSQL 16.
