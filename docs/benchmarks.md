# Benchmarks

Every number on this page was measured directly against PostgreSQL 16 and recorded from real runs. The raw data
comes from local hardware benchmarks, the live AWS EC2 host (`portfolio-apps`), and the reference specifications in
[`docs/SPEC.md`](SPEC.md).

---

## Headline numbers

| Metric | Measured Result | Context / Source |
|---|---|---|
| **Optimal debt reduction** | **10 IOUs $\rightarrow$ 3 transfers** (70% reduction) | Seeded Goa trip; beats greedy (4 transfers) |
| **Protected concurrency race** | **1 × 201, 9 × 409** (0 duplicate writes, 0 drift) | 10 concurrent requests to record same payment on EC2 |
| **Rage-click replay** | **1 × 201, 9 × 200 replays** (1 receipt ID) | 10 concurrent requests with identical Idempotency-Key on EC2 |
| **Naive mode race drift** | **Sum = ₹0.00, ±₹45,000 balance drift** | 10 concurrent naive payments on EC2 |
| **WebSocket fan-out** | **6 of 6 sockets updated** across both workers | End-to-end multi-worker Redis Pub/Sub broadcast on EC2 |
| **Full-room snapshot build** | **49.7 ms** (local) / **478 ms** (EC2 throttled, before the statistics fix below) | Room at max capacity (50 members, 10,000 expenses) |
| **Poll with a current ETag** | **0.8 ms** (local, in-process) | `304` answered from the room row; no snapshot is built |
| **History page query** | **3.0 ms** (local) / **109 ms** (EC2 throttled) | 50 items with keyset cursor pagination across 4 tables |
| **Search query latency** | **3.0 ms** median, 0.8–17.4 ms (local) / **14.6 ms** (EC2 throttled) | Prefix full-text search across 10,000 expenses |
| **Sequential write throughput** | **29.8 ms** (local) / **89.7 ms** (EC2 throttled) | Sequential HTTP writes against live app |
| **Database footprint at caps** | **32 MB** (local) / **25 MB** (EC2) | One room at maximum configured limits |
| **Deploy on the host** | **18 seconds** | `deploy.sh`: build, migrate, swap, smoke test (the whole deploy job: 29 s) |

---

## Environments

| Parameter | Local Environment | AWS EC2 Production Host | SPEC §12 Reference |
|---|---|---|---|
| **Host** | Laptop: 13th Gen Intel Core i7-13700H (20 threads) | AWS EC2 `t3.small` (ap-southeast-2) | Reference container (2 vCPU) |
| **CPU / Arch** | x86_64, 14 cores (6P + 8E), 20 vCPUs | 2 vCPU Intel Xeon Platinum 8259CL | 2 vCPU |
| **RAM / Swap** | 16 GB RAM | 2 GiB RAM + 2 GiB swap file | — |
| **CPU Credit Mode**| — | Standard mode (measured at **0 credits / 20% baseline**) | — |
| **Operating System**| Ubuntu 26.04 LTS | Ubuntu 24.04 LTS | — |
| **PostgreSQL** | PostgreSQL 16 + pgvector 0.8.7 on Docker Compose | PostgreSQL 16 with pgvector 0.8.7 (`deploy/portfolio/`) | PostgreSQL 16 |
| **Redis** | Redis 8 on Docker Compose (`redis:8-alpine`) | Redis 8 on Docker Compose (`redis:8-alpine`) | — |
| **Reverse Proxy** | None (direct port 8300) | Caddy 2 (`caddy:2.11.6`) with TLS & HTTP/2 | — |
| **Python Stack** | Python 3.12, FastAPI, Uvicorn (2 workers) | Python 3.12, FastAPI, Uvicorn (2 workers) | Python 3.12 |

> [!NOTE]
> The EC2 instance was measured when its CPU credit balance had reached **0**, meaning the virtual machine was throttled to its standard **20% baseline performance ceiling**. These are worst-case baseline numbers for the host, not burst-speed benchmarks.

---

## Big room load check (`tests/load/big_room.py`)

The load benchmark fills a single group room to its maximum configured capacity using asynchronous PostgreSQL `COPY`:
- **50 members**
- **9,900 expense versions** with ~6 splits each (59,566 total split rows)
- **9,900 settlements**
- **1,000 reversals**

After the bulk load it runs `ANALYZE`, then clears in-memory LRU snapshot caches before each run, measures snapshot construction, queries history, executes 5 distinct search shapes, and runs 20 sequential HTTP writes against the live application.

> [!NOTE]
> **Why `ANALYZE`.** Straight after a bulk `COPY` the planner's statistics still describe an empty room ("rows=1"), so it
> picks nested loops and the integrity queries run about 8× slower than they would in a live room, whose statistics
> autovacuum keeps current as it grows. Earlier runs measured that artifact: **276 ms** snapshot builds, of which
> **211 ms** was the integrity queries. With statistics refreshed the same build is **49.7 ms** (integrity **38.8 ms**).
> The searches move the other way (stale statistics happened to favour an index walk that stops at 50 rows), so they
> are reported as measured with real statistics. The EC2 numbers below predate this step and were not re-measured.

### Local vs reference comparison

Measured against local Docker Compose on Intel Core i7-13700H:

| Operation | Target Rows | Measured Local Median | SPEC §12 Reference (2 vCPU) |
|---|---|---|---|
| **Snapshot build (median of 5 runs, cold cache)** | — | **49.7 ms** | **154 ms** |
| ↳ Pairwise SQL (`OWED_SQL`) | — | 20.2 ms | 49 ms |
| ↳ Integrity queries (`INTEGRITY_SQL`) | — | 38.8 ms | 100 ms |
| ↳ Python plans (Greedy + Exact bitmask DP) | — | 0.2 ms | — |
| **History page (50 items with keyset cursor)** | 50 | **3.0 ms** | **9 ms** |
| **Searches median (across 5 query shapes)** | 50 | **3.0 ms** | **10–42 ms** |
| ↳ Prefix query: `vil` | 50 | 10.7 ms | — |
| ↳ Two-word prefix query: `seafood din` | 50 | 17.4 ms | — |
| ↳ Payer filter: `paid_by` | 50 | 1.9 ms | — |
| ↳ Amount range filter: `50.00`–`70.00` | 50 | 3.0 ms | — |
| ↳ Deleted items query: `include_deleted=true` | 50 | 0.8 ms | — |
| **Writes (20 sequential HTTP writes via app)** | — | **29.8 ms** | **22.5 ms** |
| **Total database size (whole dev database, room at caps)** | — | **46 MB** | **17.6 MB** |

Ledger invariants (`zero_sum_paise == 0`, `drift == 0`, `bad_split_expenses == 0`, `orphan_edits == 0`) are validated before and after the 20 writes.

### On the live EC2 host (`portfolio-apps`, 20% CPU baseline)

Measured on the production host on 2026-10-29 with zero accumulated CPU credits:

| Operation | Measured Median | Notes |
|---|---|---|
| **Snapshot build** | **478 ms** | Cache cleared; Pairwise SQL 130 ms, Integrity queries 310 ms |
| **History page (50 items)** | **109 ms** | Keyset pagination across 4 tables |
| **Searches (5 query shapes)** | **14.6 ms** | Range: 5.3 ms to 40.8 ms |
| **Writes (20 sequential HTTP)** | **89.7 ms** | Fresh TCP connection per request via Caddy |
| **Database footprint** | **25 MB** | Group at full 10,000 expense capacity |

Throttled numbers are sensitive to CPU credit recovery: an extended benchmark run after two consecutive full-room fills measured writes at 449 ms and searches at 80 ms.

Live container memory utilization (`docker stats`):
- `splitsync-app`: **168 MB** (2 workers)
- `postgres`: **114 MB**
- `caddy`: **49 MB**
- `redis`: **8 MB**

---

## Exact solver scaling (LeetCode 465)

The spec's measurements of the $O(2^n \cdot n)$ bitmask dynamic programming solver in [`app/domain/simplify.py`](../app/domain/simplify.py) (CPython, 2 vCPU dev container) as the non-zero balance count ($n$) increases:

| Non-Zero Balances ($n$) | Bitmask States ($2^n$) | Execution Time (CPython 3.12) | Decision |
|---|---|---|---|
| 12 | 4,096 | 3.7 ms | Optimal |
| 13 | 8,192 | 12.8 ms | Optimal |
| 14 | 16,384 | 22.2 ms | Optimal |
| **15** | **32,768** | **35.6 ms** | **Optimal (Threshold)** |
| 16 | 65,536 | 75.4 ms | Fallback to Greedy |

When $n \le 15$, SplitSync recommends the exact optimal plan. Above 15, the solver falls back to the two-heap greedy algorithm ($O(N \log N)$): the big-room check above, with 50 non-zero balances, spent **0.4 ms** in Python plans.

---

## Concurrency and chaos results (EC2 Live, 2026-10-29)

Tests executed against the live deployment over HTTP/2:

1. **Protected Race (10 devices, 1 payment):**
   10 concurrent HTTP requests fire simultaneously to record Karthik paying Aarav ₹5,000.
   - Result: **1 × 201 Created, 9 × 409 Conflict (`STALE_GRAPH_VERSION`)**.
   - Ledger drift: **₹0.00**.
2. **Rage-Click / Retries (10 identical requests):**
   10 concurrent requests fire with the same `Idempotency-Key`.
   - Result: **1 × 201 Created, 9 × 200 OK replays** (`Idempotent-Replayed: true`).
   - Recorded rows: **Exactly 1 row** in `settlements`.
3. **Naive Mode Race (10 devices, no locks):**
   10 concurrent requests fire in naive mode (`settings.ENABLE_NAIVE_MODE = True`).
   - Result: **10 rows inserted**.
   - Net balance sum badge: **₹0.00 (Green)**.
   - Ledger drift: **Aarav +₹45,000, Karthik −₹45,000 (Red alert)**.
4. **Multi-Worker WebSocket Fan-Out:**
   Sockets connected across both independent Uvicorn worker processes.
   - Result: **6 of 6 WebSockets across both workers received the new versioned snapshot** via Redis Pub/Sub.

---

## Deployment and smoke benchmarks

- **Deploy on the host:** **18 seconds** (`deploy.sh`: build, migrate, swap, smoke test); the whole GitHub Actions deploy job took 29 s. CI before it takes about 2 minutes.
- **Smoke validation checks:**
  - Graph version: **9**
  - Pairwise IOUs: **10**
  - Greedy transfers: **4**
  - Optimal transfers: **3**
  - Net balance zero-sum: **₹0.00**
  - Ledger drift total: **0 paise**

---

## Run them yourself

### 1. Run unit and domain tests

```bash
uv run pytest tests/unit -q
```
Runs 255 tests in ~3 seconds, verifying integer money math, Hamilton splits, bitmask DP solver, and static asset security.

### 2. Run integration tests

```bash
uv run pytest tests/integration -q
```
Verifies protected and naive concurrency races, edit/delete reversals, keyset pagination, prefix search, and rate limiting against PostgreSQL and Redis.

### 3. Run multi-worker WebSocket fan-out test

Ensure the local Docker Compose stack is running:
```bash
docker compose up -d --build --wait
uv run pytest tests/e2e -q
```
Opens WebSockets until both worker PIDs are observed, executes a write, and verifies that both workers broadcast the snapshot.

### 4. Run the big room load check

```bash
PYTHONPATH=. uv run python tests/load/big_room.py
```
Fills one group to its caps with `COPY`, benchmarks cold snapshot construction, history pagination, and 5 search queries, executes 20 live HTTP writes, verifies ledger invariants, and tears down the room.

### 5. Run the deployment smoke test

```bash
uv run python -m app.smoke http://localhost:8300
```
Creates a fresh demo room, queries its state, and asserts that the Goa trip seed produces exactly 10 IOUs $\rightarrow$ 3 optimal transfers with zero ledger drift.
