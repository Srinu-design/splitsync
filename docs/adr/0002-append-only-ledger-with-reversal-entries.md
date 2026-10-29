# ADR-0002: Append-only ledger with reversal entries; balances are a cache

Status: Accepted · Date: 2026-10-29

## Context

Typical expense trackers use mutable database rows: editing an expense runs SQL `UPDATE`, and deleting an expense runs SQL `DELETE`. This design destroys historical audit trails. When an accidental deletion or inaccurate edit occurs, the previous state is permanently lost. Implementing "Undo" requires separate audit log tables that frequently desynchronize from primary tables.

Furthermore, updating balances purely via event replay from genesis scales poorly: as an active room accumulates thousands of expense records, computing current balances would require rescanning the entire history table on every read.

## Decision

The ledger is append-only. No write path runs `UPDATE` or `DELETE` on rows in `expenses`, `expense_splits`, `settlements` or `reversals`. The only deletions remove a whole room's rows: resetting a demo room, and the sweep deleting expired rooms.

- **Reversals for edits and deletes:**
  - Deleting an expense inserts a row into `reversals` with `reason = 'DELETE'`, referencing `expense_id`.
  - Editing an expense inserts a reversal row with `reason = 'EDIT'` and simultaneously inserts a new version into `expenses` referencing `revision_of = :old_expense_id`.
  - Restoring an expense inserts a fresh row into `expenses` copying the deleted row's attributes with `revision_of = :deleted_expense_id`.
- **Views for active records:**
  - Read queries query `active_expenses` and `active_settlements`, which filter out reversed items using `WHERE NOT EXISTS (SELECT 1 FROM reversals r WHERE r.expense_id = e.id)`.
- **Cached member balances:**
  - `members.net_balance_paise` acts as a fast, denormalized balance cache.
  - Every write transaction atomically updates `net_balance_paise` using increment/decrement arithmetic deltas in the same database transaction.
  - Ledger integrity checks recompute expected balances directly from `active_expenses` and `active_settlements` to detect any cache drift (`tests/integration/test_edit_delete.py`).

## Alternatives considered

- **In-place `UPDATE` and `DELETE`.** Loses history, makes undo impossible, and allows racing writes to overwrite previous changes without a trace.
- **Pure Event Sourcing without cached balances.** Eliminates denormalized balance state, but computing a room snapshot on every HTTP read would require full table aggregations, degrading latency to hundreds of milliseconds.
- **Separate audit shadow table.** Dual-writes to primary and audit tables add complexity and risk discrepancies when migrations alter table structures.

## Consequences

- Full history is preserved by design. Every previous version, deletion, and restoration is recorded and inspectable via the activity feed.
- One-tap "Undo" comes for free: restoring a deleted expense is simply writing another append-only record.
- Balances are one indexed read of `members.net_balance_paise`, not a scan of the ledger.
- Reversal rows consume additional storage, but the caps (10,000 expense versions and 10,000 settlements per group) bound it: a full group measured 25–32 MB.

## When we would revisit

If table bloat from extreme reversal churn exceeds disk storage thresholds, requiring a compaction or archival pipeline for closed groups older than several years.
