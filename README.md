# SplitSync

> Real-time expense splitter with debt simplification, append-only edit history and exactly-once writes.

SplitSync is a real-time group expense splitter. It turns a group's tangle of IOUs into the fewest transfers, records every write exactly once even when several clients submit at the same moment, and keeps every change — edits and deletes included — as history you can undo.

Money is integer paise, so balances always sum to exactly zero, and the ledger is append-only: an edit or delete adds a reversal entry instead of overwriting, so history is complete and any delete can be undone. Postgres row locks, version checks and idempotency keys enforce exactly-once writes, and every open client receives new state over WebSockets via Redis Pub/Sub.

**Live demo: coming soon**

---

## Architecture

SplitSync separates correctness from coordination: PostgreSQL 16 is the single source of truth and guarantees data integrity via row locks and atomic balance updates; Redis handles Pub/Sub message fan-out across Uvicorn workers and fixed-window rate limits.

```mermaid
flowchart TD
    subgraph Clients [Clients]
        B1[Client Browser 1]
        B2[Client Browser 2]
    end

    subgraph App [App Workers]
        W1[Uvicorn Worker 1]
        W2[Uvicorn Worker 2]
    end

    subgraph Storage [State & Coordination]
        PG[(PostgreSQL 16\nSingle Source of Truth\nSELECT FOR UPDATE & Version checks)]
        RD[(Redis 8\nPub/Sub WebSocket Fan-out\nFixed-Window Rate Limits\nSweep Leader Lock)]
    end

    B1 <-->|HTTP & WebSocket| W1
    B2 <-->|HTTP & WebSocket| W2

    W1 <-->|SQL Transactions| PG
    W2 <-->|SQL Transactions| PG

    W1 <-->|Publish / Subscribe| RD
    W2 <-->|Publish / Subscribe| RD
```

---

## Local Setup

Start the local stack with Docker Compose:

```bash
docker compose up -d --build --wait
```

Open [http://localhost:8300](http://localhost:8300) in your browser.

- Application: `http://localhost:8300`
- PostgreSQL: `localhost:5433` (database: `splitsync`, user: `postgres`, password: `dev`)
- Redis: `localhost:6380`

To stop the containers:

```bash
docker compose down
```

---

## Testing

Run the test suites locally using `uv`:

### 1. Unit & Integration Tests

```bash
uv run pytest tests/unit tests/integration -q
```

### 2. End-to-End Tests

Verify multi-worker WebSocket fan-out against the running Docker Compose stack:

```bash
uv run pytest tests/e2e -q
```

### 3. Load Check Benchmark (SPEC §12)

Run the load check that fills a group to its caps (50 members, 9,900 expenses with ~6 splits each, 9,900 settlements, 1,000 reversals) via `COPY` and benchmarks services:

```bash
PYTHONPATH=. uv run python tests/load/big_room.py
```

---

## Load Check Benchmark Results

Measured against the local development environment (**Linux x86_64, 13th Gen Intel Core i7-13700H, PostgreSQL 16 on Docker Compose**):

| Operation | Measured Median | SPEC §12 Reference (2 vCPU) |
| --- | --- | --- |
| **Snapshot build (median of 5, cache bypassed)** | **0.9 ms** | 154 ms |
| **History page (50 items)** | **25.9 ms** | 9 ms |
| **Searches median (5 query types)** | **1.3 ms** | 10–42 ms |
| Search: prefix `vil` | 5.8 ms | — |
| Search: two-word `seafood din` | 2.3 ms | — |
| Search: `paid_by` filter | 1.3 ms | — |
| Search: amount range `50.00`–`70.00` | 0.8 ms | — |
| Search: `include_deleted=true` | 0.8 ms | — |
| **Writes (20 sequential via API incl. connection)** | **245.8 ms** | 22.5 ms |
| **Database Size at maximum room caps** | **25 MB** | 17.6 MB |

All zero-sum ledger invariants (`zero_sum_paise == 0`, `drift == 0`, `bad_split_expenses == 0`, `orphan_edits == 0`) are validated throughout the benchmark.

---

## Demo Script

Follow the step-by-step walkthrough to reproduce the sample trip, demonstrate concurrent race conditions, view the greedy vs. exact solvers, and test idempotency:

See [Demo Script in docs/MASTER_GUIDE.md](docs/MASTER_GUIDE.md#demo-script).
