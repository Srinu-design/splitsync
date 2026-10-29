# ADR-0004: Idempotency keys: re-checked inside the lock, partial unique index, IETF codes 400/409/422

Status: Accepted · Date: 2026-10-29

## Context

Mobile networks suffer from transient packet loss: a client submits a payment, the server commits the transaction, but the HTTP response drops before reaching the client. The client automatically retries. Without idempotency protection, the retry creates a duplicate payment, double-counting financial transactions.

A naive pre-lock idempotency check suffers from check-then-act race conditions: ten identical requests arrive at once, none find an existing key, all ten queue on the lock, and all ten proceed to execute.

## Decision

Every mutating endpoint (`POST /expenses`, `POST /settlements`, `PUT /expenses/{id}`, `DELETE /expenses/{id}`, `POST /expenses/{id}/restore`, `DELETE /settlements/{id}`) requires an `Idempotency-Key` header (16 to 64 ASCII characters matching `^[A-Za-z0-9_-]{16,64}$`).

- **IETF draft error codes:**
  - Missing or invalid key: `400 IDEMPOTENCY_KEY_MISSING`.
  - Stale room state: `409 STALE_GRAPH_VERSION` / `EXPENSE_CHANGED`.
  - Reusing a key with a mutated payload: `422 IDEMPOTENCY_KEY_REUSED`.
  - Successful replay: `200 OK` with header `Idempotent-Replayed: true` and the original JSON receipt.
- **Re-checked inside the lock:**
  In [`app/services/shared.py lookup_idempotency()`](../../app/services/shared.py), the key check occurs **after** acquiring the exclusive room lock (`SELECT ... FOR UPDATE`). When a race occurs, the first request acquires the lock, commits the record and key, and releases the lock. Subsequent requests acquire the lock, discover the committed row, verify the SHA-256 `payload_hash`, and return the stored receipt.
- **Database constraints:**
  Unique constraints serve as the database backstop:
  - `expenses`: `UNIQUE (group_id, idempotency_key)`
  - `reversals`: `UNIQUE (group_id, idempotency_key)`
  - `settlements`: `CREATE UNIQUE INDEX settlements_idem_protected ON settlements (group_id, idempotency_key) WHERE mode = 'PROTECTED';` (partial index allows naive demonstration mode to duplicate rows).

## Alternatives considered

- **Checking idempotency before acquiring the lock.** Fails under concurrent bursts: racing requests all pass the check before the first request writes the key.
- **Storing idempotency keys in Redis.** Fast, but creates a dual-write vulnerability: if Redis fails or restarts, keys are lost while database records remain, allowing duplicate operations.
- **Returning 409 Conflict when a key is reused with a different payload.** The IETF draft specification for HTTP idempotency specifies `422 Unprocessable Entity` when an idempotency key is reused for an altered request.

## Consequences

- 10 concurrent requests with the same idempotency key result in exactly 1 write (`201 Created`) and 9 replays (`200 OK` with `Idempotent-Replayed: true`) returning the exact same receipt ID.
- Reusing an existing key with a different amount or participant fails deterministically with `422 IDEMPOTENCY_KEY_REUSED`.
- Validated in [`tests/integration/test_expenses.py`](../../tests/integration/test_expenses.py) and [`tests/integration/test_settlements.py`](../../tests/integration/test_settlements.py).

## When we would revisit

If idempotency keys require global expiration independent of room retention lifecycles, requiring an asynchronous background key-pruning partition table.
