# ADR-0010: graph_version as the history clock; keyset pages; scoped prefix search without GIN

Status: Accepted · Date: 2026-10-29

## Context

Users need to review past activity (who added which expense, who paid whom, what was edited or undone) and search past expenses by keyword, payer, amount, or date.

An activity feed spans four separate tables: `expenses`, `settlements`, `reversals`, and `members`. Paging through multi-table unions using `OFFSET` causes $O(N)$ performance degradation and returns duplicate or missing entries if new writes occur during pagination.

Similarly, full-text search across multi-tenant databases typically uses PostgreSQL GIN indexes. However, a global GIN index indexes words across all rooms simultaneously, leading to index bloat and slow writes.

## Decision

- **`graph_version` as the unified history clock:**
  Every state write increments `groups.graph_version` and stamps the corresponding row. This provides a total order across all four tables: `expenses` (rank 3), `settlements` (rank 2), `reversals` (rank 1), and `members` (rank 0).
- **Keyset cursor pagination:**
  In [`app/services/history.py`](../../app/services/history.py), the pagination cursor encodes `version.rank.id` in base64. Each subquery in the `UNION ALL` applies an index filter `(version, rank, id) < (:cv, :ck, :cid)` and an individual `LIMIT :lim`. The outer query unions and cuts at `:lim`. This bounds the rows scanned to at most $4 \times (\text{limit} + 1)$, running in **5.1 ms local** and **9 ms reference**.
- **Prefix search without a GIN index:**
  In [`app/services/search.py`](../../app/services/search.py), full-text search uses PostgreSQL's `'simple'` dictionary:
  ```sql
  to_tsquery('simple', (SELECT string_agg(quote_literal(t) || ':*', ' & ') FROM unnest(CAST(:tokens AS text[])) AS t))
  ```
  Searches match word prefixes (e.g., `"seafood din"` matches `"Beach-shack seafood dinner"`).
  **No GIN index is created.** Because all queries are scoped to a single room (`WHERE group_id = :gid`), the composite B-tree index on `(group_id, spent_on DESC, id DESC)` narrows the candidate set to at most 10,000 rows. Searching within this in-memory B-tree subset takes **10.4 ms local** (10–42 ms reference), avoiding GIN write amplification and VACUUM overhead.

## Alternatives considered

- **Global GIN index on `title_tsv`.** Slows down every expense write, requires intensive background vacuuming, and offers minimal query speedup for room-scoped searches.
- **Timestamp-based cursors (`created_at`).** Multiple rows created in the same database transaction share identical timestamps, causing erratic pagination order and skipped items.
- **`OFFSET / LIMIT` pagination.** Degrades linearly with page depth and produces phantom skips when concurrent users add expenses.

## Consequences

- The activity feed pages smoothly with zero drift or duplicated items regardless of concurrent write traffic.
- Prefix search on a full room measured 5–25 ms locally and 5–41 ms on the throttled EC2 host.
- Validated in [`tests/unit/test_query_cursors.py`](../../tests/unit/test_query_cursors.py), [`tests/integration/test_history.py`](../../tests/integration/test_history.py), and [`tests/integration/test_search.py`](../../tests/integration/test_search.py).

## When we would revisit

If group size limits expand to 500,000 expenses per room, where room-scoped B-tree scans exceed 50 ms and warrant room-partitioned GIN or trigram indexes.
