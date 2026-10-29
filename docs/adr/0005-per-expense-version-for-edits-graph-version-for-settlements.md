# ADR-0005: Per-expense version for edits, room-wide graph_version for settlements

Status: Accepted · Date: 2026-10-29

## Context

Financial mutations have different dependency scopes. A debt settlement depends on the net balance of both the payer and payee, which in turn depends on every expense and settlement ever recorded in the room. If anyone adds an expense while a settlement is being drafted, the debt calculations change.

In contrast, editing an expense (e.g. correcting a typo in a dinner bill) depends only on that specific expense. Applying a room-wide version check to expense edits would cause high friction: if Alice is editing the dinner bill while Bob records a cab fare on the other side of town, Alice's edit would be rejected with a version conflict, even though their changes are entirely independent.

## Decision

Use different versioning scopes for settlements and expense modifications:

- **Settlements require room-wide `graph_version`:**
  - `POST /settlements` requires `expected_graph_version`.
  - The transaction verifies that `room["graph_version"] == body.expected_graph_version`.
  - If any mutation occurred since the client loaded their screen, the settlement is rejected with `409 STALE_GRAPH_VERSION`, requiring the user to review the updated plan before paying.
- **Edits and deletes require per-expense versioning:**
  - `PUT /expenses/{id}`, `DELETE /expenses/{id}`, and `POST /expenses/{id}/restore` target the specific `expense_id` displayed on the user's screen.
  - Inside the room lock, the service verifies whether that specific version has already been reversed ([`app/services/expenses.py`](../../app/services/expenses.py)):
    ```python
    if target["reversal_reason"] is not None:
        await expense_changed(connection, room, target)
    ```
  - If another user edited or deleted that expense first, the server returns `409 EXPENSE_CHANGED`, accompanied by `current_expense_id` and a `deleted` boolean flag. The client UI uses this metadata to automatically reload and open the editor on the newest version.
  - An unrelated write elsewhere in the room does not block an edit.

## Alternatives considered

- **Room-wide version checks for everything.** Rejected because concurrent users adding or editing independent expenses in an active room would constantly reject each other with false conflicts.
- **No version check for edits (last-write-wins).** Rejected because if two users simultaneously edit the same expense with conflicting amounts, the slower write would silently overwrite the faster write without warning.

## Consequences

- High concurrency on independent items: multiple group members can edit different expenses simultaneously without conflicts.
- Strong safety on shared items: two users editing the same expense version result in 1 commit (`200 OK`) and 1 rejection (`409 EXPENSE_CHANGED`).
- Settlements are protected against paying outdated debts.
- Validated in [`tests/integration/test_edit_delete.py`](../../tests/integration/test_edit_delete.py).

## When we would revisit

If introducing multi-currency settlement chains or conditional cross-expense split dependencies.
