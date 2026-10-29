# ADR-0003: Postgres owns correctness: room row lock plus version checks

Status: Accepted · Date: 2026-10-29

## Context

Collaborative expense splitting involves concurrent writes: friends submitting expenses simultaneously, roommates recording the same UPI settlement at the same moment, and network retries. In-memory locks (such as Python `asyncio.Lock()`) only coordinate within a single operating system process; with multiple Uvicorn workers across CPU cores, in-memory locks provide zero cross-worker protection.

Relying exclusively on distributed locks in Redis introduces failure modes: if Redis restarts or experiences network partitions, safety guarantees are compromised.

## Decision

PostgreSQL 16 is the single source of truth and exclusively owns transactional correctness.

- **Pessimistic room lock:**
  Every write transaction begins by acquiring an exclusive row lock on the group row:
  ```sql
  SELECT id, kind, graph_version FROM groups WHERE slug = :slug AND expires_at > now() FOR UPDATE
  ```
  This serializes in-flight concurrent writes targeting the same room across all Uvicorn worker processes.
- **Optimistic version check:**
  While pessimistic locking stops overlapping execution, it cannot prevent double-submissions that occur seconds apart. `POST /settlements` requires `expected_graph_version`. If a roommate submits a payment based on an outdated balance sheet, the version mismatch triggers `409 STALE_GRAPH_VERSION`.
- **Lock ordering & deadlock handling:**
  To prevent lock cycles across concurrent operations, transactions follow a strict global lock hierarchy:
  1. Room row lock (`groups ... FOR UPDATE`).
  2. Member row locks in ascending order of primary key ID (`ORDER BY id`).
  In [`app/db.py write_tx()`](../../app/db.py), transactions configure `SET LOCAL lock_timeout = '2s'`. Lock timeouts raise SQLSTATE `55P03`, mapped to `409 GROUP_BUSY`. Serialization failures (`40001`) and deadlocks (`40P01`) are tried up to 3 times in all, with a random 50–200 ms pause between tries.

## Alternatives considered

- **In-process locks (`asyncio.Lock`).** Fails completely in multi-worker production deployments (2 Uvicorn workers).
- **Redis distributed lock (`SET NX PX`).** Coupling correctness to Redis means a Redis failure or network hiccup compromises database integrity.
- **Optimistic concurrency control only (no `FOR UPDATE`).** High-contention workloads (e.g. 10 members submitting expenses simultaneously) result in excessive transaction rollbacks and poor client experience.

## Consequences

- Exactly-once transactional execution is guaranteed by the ACID database engine.
- Simultaneous writes to different rooms run in parallel without blocking each other.
- Concurrency test [`tests/integration/test_settlements.py`](../../tests/integration/test_settlements.py) confirms that 10 concurrent requests to record the same payment result in exactly 1 commit (`201 Created`) and 9 rejections (`409 STALE_GRAPH_VERSION`), with ₹0 drift.

## When we would revisit

If write throughput on a single room exceeds 50 writes/second continuously, requiring sub-room partition locking or sharded balance logs.
