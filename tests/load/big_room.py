"""tests/load/big_room.py — Load check per SPEC §12 and §14.

Fills one GROUP to its caps:
  - 50 members
  - 9,900 expense versions with ~6 splits each
  - 9,900 settlements
  - 1,000 reversals
using asyncpg COPY directly against DATABASE_URL.
Keeps balances consistent so ledger invariants stay 0.
Times snapshot build (bypassing LRU cache before each run), history page,
5 searches, and 20 writes via real HTTP against the running app.
Prints a table of medians, search row counts, snapshot split, and database size,
asserts invariants after the writes, then cleans up.
"""

import asyncio
import hashlib
import json
import os
import secrets
import statistics
import time
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.domain.simplify import pairwise_edges, plans
from app.services.history import list_activity
from app.services.search import find_expenses
from app.services.state import (
    BAD_SPLITS_SQL,
    INTEGRITY_SQL,
    ORPHAN_EDITS_SQL,
    OWED_SQL,
    _caches,
    build_snapshot,
)


def get_pg_dsn(url: str) -> str:
    # Convert postgresql+asyncpg:// to postgresql:// for raw asyncpg connection
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


async def run_load_check():
    settings = get_settings()
    db_url = os.getenv("DATABASE_URL", settings.DATABASE_URL)
    base_url = os.getenv("BASE_URL", "http://localhost:8300")
    pg_dsn = get_pg_dsn(db_url)

    print("=" * 60)
    print("SplitSync Phase 8b Load Check (SPEC §12)")
    print(f"Connecting to database: {pg_dsn.split('@')[-1]}")
    print(f"Targeting HTTP API:     {base_url}")
    print("=" * 60)

    conn = await asyncpg.connect(pg_dsn)
    now = datetime.now(UTC)
    today = date.today()

    try:
        # 1. Create one GROUP
        slug = secrets.token_urlsafe(8)  # 11 characters
        group_name = "Load Check Group 50 Members"
        # 1 initial + 9900 expenses + 9900 settlements + 1000 reversals = 20801
        initial_graph_version = 20801
        expires_at = now + timedelta(days=settings.GROUP_TTL_DAYS)

        group_id = await conn.fetchval(
            """
            INSERT INTO groups (
                slug, kind, name, graph_version, created_at, last_active_at, expires_at
            )
            VALUES ($1, 'GROUP', $2, $3, $4, $4, $5)
            RETURNING id
            """,
            slug,
            group_name,
            initial_graph_version,
            now,
            expires_at,
        )
        print(f"Created group id={group_id}, slug={slug}")

        # 2. 50 members
        member_records = []
        for i in range(1, 51):
            color = f"#{((i * 123456) & 0xFFFFFF):06x}"
            member_records.append((group_id, 1, f"Member {i:02d}", color, 0, now))

        member_rows = await conn.fetch(
            """
            INSERT INTO members (group_id, version, name, color, net_balance_paise, created_at)
            SELECT $1, x.version, x.name, x.color, x.net_balance_paise, x.created_at
            FROM jsonb_to_recordset($2::jsonb) AS x(
                version bigint,
                name text,
                color text,
                net_balance_paise bigint,
                created_at timestamptz
            )
            RETURNING id, name
            """,
            group_id,
            json.dumps(
                [
                    {
                        "version": r[1],
                        "name": r[2],
                        "color": r[3],
                        "net_balance_paise": r[4],
                        "created_at": r[5].isoformat(),
                    }
                    for r in member_records
                ]
            ),
        )
        member_ids = [row["id"] for row in member_rows]
        assert len(member_ids) == 50
        print(f"Inserted {len(member_ids)} members")

        # Track net balance changes: member_id -> net paise
        net_balances = {m_id: 0 for m_id in member_ids}

        # 3. 9,900 expenses
        start_exp_id = await conn.fetchval(
            "SELECT nextval('expenses_id_seq') FROM generate_series(1, 9900) LIMIT 1"
        )
        await conn.execute("SELECT nextval('expenses_id_seq') FROM generate_series(1, 9899)")
        expense_ids = list(range(start_exp_id, start_exp_id + 9900))

        expense_records = []
        split_records = []

        titles = [
            "Villa booking (2 nights)",
            "Beach-shack seafood dinner",
            "Scooter rentals in Goa",
            "Dudhsagar jeep safari tour",
            "Parasailing adventure trip",
            "Cafe brunch with friends",
            "Club night drinks and snacks",
            "Groceries and beach snacks",
            "Airport taxi transfer trip",
            "Island boat tour excursion",
        ]

        total_paise_per_exp = 6000  # ₹60.00 split 6 ways = 1000 paise each
        splits_count = 6
        share_paise = total_paise_per_exp // splits_count

        for idx, exp_id in enumerate(expense_ids):
            version = 1 + idx
            payer_idx = idx % 50
            payer_id = member_ids[payer_idx]
            title = titles[idx % len(titles)]

            payload = {"title": title, "amount": "60.00"}
            payload_hash = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            idem_key = f"load-exp-{idx:06d}-idem-key-val"
            resp_json = json.dumps({"expense_id": exp_id, "graph_version": version})

            expense_records.append(
                (
                    exp_id,
                    group_id,
                    version,
                    exp_id,  # lineage_id = id
                    None,  # revision_of = None
                    payer_id,
                    None,  # acted_by
                    title,
                    today - timedelta(days=idx % 30),
                    total_paise_per_exp,
                    "EQUAL",
                    idem_key,
                    payload_hash,
                    resp_json,
                    now,
                )
            )

            # 6 splits for this expense
            net_balances[payer_id] += total_paise_per_exp
            for s_idx in range(splits_count):
                part_id = member_ids[(idx + s_idx) % 50]
                split_records.append((group_id, exp_id, part_id, None, share_paise))
                net_balances[part_id] -= share_paise

        print("COPYing 9,900 expenses...")
        await conn.copy_records_to_table(
            "expenses",
            records=expense_records,
            columns=[
                "id",
                "group_id",
                "version",
                "lineage_id",
                "revision_of",
                "paid_by",
                "acted_by",
                "title",
                "spent_on",
                "total_paise",
                "split_type",
                "idempotency_key",
                "payload_hash",
                "response_json",
                "created_at",
            ],
        )

        print(f"COPYing {len(split_records)} splits...")
        await conn.copy_records_to_table(
            "expense_splits",
            records=split_records,
            columns=["group_id", "expense_id", "member_id", "weight", "share_paise"],
        )

        # 4. 1,000 reversals (reverse the first 1,000 expenses)
        reversal_records = []
        for r_idx in range(1000):
            rev_exp = expense_records[r_idx]
            rev_exp_id = rev_exp[0]
            version = 9901 + r_idx
            idem_key = f"load-rev-{r_idx:06d}-idem-key-val"
            payload_hash = "0" * 64

            reversal_records.append(
                (
                    group_id,
                    version,
                    rev_exp_id,
                    None,  # settlement_id
                    "DELETE",
                    None,  # acted_by
                    idem_key,
                    payload_hash,
                    "{}",
                    now,
                )
            )

            # Reverse ledger effect of this expense on balances
            payer_id = rev_exp[5]
            net_balances[payer_id] -= total_paise_per_exp
            for s_idx in range(splits_count):
                part_id = member_ids[(r_idx + s_idx) % 50]
                net_balances[part_id] += share_paise

        print("COPYing 1,000 reversals...")
        await conn.copy_records_to_table(
            "reversals",
            records=reversal_records,
            columns=[
                "group_id",
                "version",
                "expense_id",
                "settlement_id",
                "reason",
                "acted_by",
                "idempotency_key",
                "payload_hash",
                "response_json",
                "created_at",
            ],
        )

        # 5. 9,900 settlements
        settlement_records = []
        settle_amt = 500  # ₹5.00
        for s_idx in range(9900):
            version = 10901 + s_idx
            m_from = member_ids[s_idx % 50]
            m_to = member_ids[(s_idx + 1) % 50]
            idem_key = f"load-stl-{s_idx:06d}-idem-key-val"
            payload_hash = "0" * 64

            settlement_records.append(
                (
                    group_id,
                    version,
                    m_from,
                    m_to,
                    None,  # acted_by
                    settle_amt,
                    "PROTECTED",
                    idem_key,
                    payload_hash,
                    version - 1,
                    "{}",
                    now,
                )
            )
            net_balances[m_from] += settle_amt
            net_balances[m_to] -= settle_amt

        print("COPYing 9,900 settlements...")
        await conn.copy_records_to_table(
            "settlements",
            records=settlement_records,
            columns=[
                "group_id",
                "version",
                "from_member",
                "to_member",
                "acted_by",
                "amount_paise",
                "mode",
                "idempotency_key",
                "payload_hash",
                "graph_version_before",
                "response_json",
                "created_at",
            ],
        )

        # 6. Update member cached balances so invariants stay 0
        assert sum(net_balances.values()) == 0, f"Sum of balances is {sum(net_balances.values())}"
        print("Updating member net balances...")
        for m_id, bal in net_balances.items():
            await conn.execute("UPDATE members SET net_balance_paise = $1 WHERE id = $2", bal, m_id)

        # Measure DB size
        db_size_bytes = await conn.fetchval("SELECT pg_database_size(current_database())")
        db_size_str = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )
        print(f"Database size: {db_size_str} ({db_size_bytes / (1024 * 1024):.1f} MB)")

        # 7. Benchmark with real application services
        engine = create_async_engine(db_url)

        # (a) Snapshot build: CLEAR LRU CACHE before each of the 5 runs
        print("Measuring snapshot build (5 real builds, cache cleared before each run)...")
        snap_times = []
        snap = None
        for _ in range(5):
            _caches.clear()  # Clear in-process LRU cache so each run is a real build
            t0 = time.perf_counter()
            snap = await build_snapshot(engine, settings, slug)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            snap_times.append(elapsed_ms)
            assert snap["invariants"]["zero_sum_paise"] == 0
            assert snap["invariants"]["bad_split_expenses"] == 0

        snapshot_median = statistics.median(snap_times)
        print(f"  Snapshot build median: {snapshot_median:.1f} ms")

        # Measure snapshot breakdown components
        async with engine.begin() as sql_conn:
            # 1. Pairwise SQL (OWED_SQL)
            t0 = time.perf_counter()
            owed_rows = (await sql_conn.execute(text(OWED_SQL), {"gid": group_id})).all()
            pairwise_sql_ms = (time.perf_counter() - t0) * 1000

            # 2. Integrity queries
            t0 = time.perf_counter()
            await sql_conn.execute(text(INTEGRITY_SQL), {"gid": group_id})
            await sql_conn.scalar(text(BAD_SPLITS_SQL), {"gid": group_id})
            await sql_conn.scalar(text(ORPHAN_EDITS_SQL), {"gid": group_id})
            integrity_ms = (time.perf_counter() - t0) * 1000

        # 3. Python plans
        owed = {(debtor, creditor): amt for debtor, creditor, amt in owed_rows}
        balances = {m["id"]: m["net_balance_paise"] for m in snap["members"]}
        t0 = time.perf_counter()
        _ = plans(balances, settings.EXACT_MAX_NONZERO)
        _ = sorted(pairwise_edges(balances, owed), key=lambda edge: (-edge[2], edge[0], edge[1]))
        python_plans_ms = (time.perf_counter() - t0) * 1000
        print(
            f"  Snapshot split: pairwise SQL {pairwise_sql_ms:.1f} ms, "
            f"integrity queries {integrity_ms:.1f} ms, Python plans {python_plans_ms:.1f} ms"
        )

        # (b) One history page of 50
        print("Measuring history page (50 items)...")
        t0 = time.perf_counter()
        history_page = await list_activity(engine, settings, slug, limit=50)
        history_ms = (time.perf_counter() - t0) * 1000
        assert len(history_page["items"]) == 50
        print(f"  History page (50): {history_ms:.1f} ms")

        # (c) Five searches (print row counts and assert each returns >= 1)
        print("Measuring 5 searches...")
        # 1. 'vil' prefix
        t0 = time.perf_counter()
        s1 = await find_expenses(engine, settings, slug, q="vil")
        s1_ms = (time.perf_counter() - t0) * 1000
        c1 = len(s1["items"])
        assert c1 >= 1, f"Search 'vil' returned {c1} rows"
        print(f"  Search 'vil': {s1_ms:.1f} ms (found {c1} rows)")

        # 2. 'seafood din' two-word
        t0 = time.perf_counter()
        s2 = await find_expenses(engine, settings, slug, q="seafood din")
        s2_ms = (time.perf_counter() - t0) * 1000
        c2 = len(s2["items"])
        assert c2 >= 1, f"Search 'seafood din' returned {c2} rows"
        print(f"  Search 'seafood din': {s2_ms:.1f} ms (found {c2} rows)")

        # 3. paid_by filter
        t0 = time.perf_counter()
        s3 = await find_expenses(engine, settings, slug, paid_by=member_ids[0])
        s3_ms = (time.perf_counter() - t0) * 1000
        c3 = len(s3["items"])
        assert c3 >= 1, f"Search paid_by returned {c3} rows"
        print(f"  Search paid_by: {s3_ms:.1f} ms (found {c3} rows)")

        # 4. amount range filter
        t0 = time.perf_counter()
        s4 = await find_expenses(engine, settings, slug, min_amount="50.00", max_amount="70.00")
        s4_ms = (time.perf_counter() - t0) * 1000
        c4 = len(s4["items"])
        assert c4 >= 1, f"Search amount range returned {c4} rows"
        print(f"  Search amount range: {s4_ms:.1f} ms (found {c4} rows)")

        # 5. include_deleted
        t0 = time.perf_counter()
        s5 = await find_expenses(engine, settings, slug, include_deleted=True)
        s5_ms = (time.perf_counter() - t0) * 1000
        c5 = len(s5["items"])
        assert c5 >= 1, f"Search include_deleted returned {c5} rows"
        print(f"  Search include_deleted: {s5_ms:.1f} ms (found {c5} rows)")

        searches_median = statistics.median([s1_ms, s2_ms, s3_ms, s4_ms, s5_ms])
        print(f"  Searches median: {searches_median:.1f} ms")

        # (d) 20 writes over REAL HTTP against the running compose app
        print(f"Measuring 20 writes via real HTTP against {base_url} (one client per write)...")
        write_times = []
        for w_idx in range(20):
            idem_key = f"load-write-{w_idx:06d}-idem-real-val"
            w_body = {
                "title": f"Bench write expense {w_idx}",
                "paid_by": member_ids[0],
                "amount": "100.00",
                "split": {
                    "type": "EQUAL",
                    "members": [{"id": m_id} for m_id in member_ids[:6]],
                },
            }
            # Time over real HTTP with one fresh client per write (including TCP handshake overhead)
            t0 = time.perf_counter()
            async with httpx.AsyncClient(base_url=base_url) as client:
                res = await client.post(
                    f"/api/rooms/{slug}/expenses",
                    headers={"Idempotency-Key": idem_key},
                    json=w_body,
                )
            elapsed = (time.perf_counter() - t0) * 1000
            assert res.status_code == 201, f"Write {w_idx} failed: {res.status_code} {res.text}"
            write_times.append(elapsed)

        write_median = statistics.median(write_times)
        print(f"  Writes (20) real HTTP median: {write_median:.1f} ms")

        # 8. Assert all invariants after the 20 writes
        _caches.clear()
        post_snap = await build_snapshot(engine, settings, slug)
        post_inv = post_snap["invariants"]
        assert post_inv["zero_sum_paise"] == 0, f"zero_sum_paise != 0: {post_inv}"
        assert post_inv["bad_split_expenses"] == 0, f"bad_splits != 0: {post_inv}"
        assert post_inv["orphan_edits"] == 0, f"orphan_edits != 0: {post_inv}"
        assert post_inv["ledger_drift_total_paise"] == 0, f"drift != 0: {post_inv}"
        print(
            "Asserted invariants after 20 writes: zero_sum=0, drift=0, bad_splits=0, orphan_edits=0"
        )

        # 9. Print Results Table
        print("\n" + "=" * 65)
        print("LOAD CHECK RESULTS (SPEC §12)")
        print("=" * 65)
        print(f"Database Size: {db_size_str}")
        print("-" * 65)
        print(f"{'Operation':<38} | {'Rows':<6} | {'Measured Median':<15}")
        print("-" * 65)
        print(f"{'Snapshot build (median of 5, real)':<38} | {'-':<6} | {snapshot_median:6.1f} ms")
        print(f"  {'Pairwise SQL':<36} | {'-':<6} | {pairwise_sql_ms:6.1f} ms")
        print(f"  {'Integrity queries':<36} | {'-':<6} | {integrity_ms:6.1f} ms")
        print(f"  {'Python plans':<36} | {'-':<6} | {python_plans_ms:6.1f} ms")
        print(f"{'History page (50 items)':<38} | {50:<6} | {history_ms:6.1f} ms")
        print(f"{'Search 1 (prefix "vil")':<38} | {c1:<6} | {s1_ms:6.1f} ms")
        print(f"{'Search 2 (two-word "seafood din")':<38} | {c2:<6} | {s2_ms:6.1f} ms")
        print(f"{'Search 3 (paid_by filter)':<38} | {c3:<6} | {s3_ms:6.1f} ms")
        print(f"{'Search 4 (amount range 50-70)':<38} | {c4:<6} | {s4_ms:6.1f} ms")
        print(f"{'Search 5 (include_deleted)':<38} | {c5:<6} | {s5_ms:6.1f} ms")
        print(f"{'Searches median':<38} | {'-':<6} | {searches_median:6.1f} ms")
        print(f"{'Writes (20 real HTTP incl. conn)':<38} | {'-':<6} | {write_median:6.1f} ms")
        print("=" * 65 + "\n")

        await engine.dispose()

    finally:
        # 10. Clean up test room
        print(f"Cleaning up load check room {slug}...")
        await conn.execute("DELETE FROM groups WHERE slug = $1", slug)
        await conn.close()
        print("Clean up complete.")


if __name__ == "__main__":
    asyncio.run(run_load_check())
