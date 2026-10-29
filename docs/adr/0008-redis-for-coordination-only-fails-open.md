# ADR-0008: Redis for coordination only, everything fails open

Status: Accepted · Date: 2026-10-29

## Context

In a multi-worker web application, worker processes need cross-process coordination: broadcasting WebSocket updates to clients connected to other workers, tracking sliding-window rate limit counters across IP addresses, and ensuring only one worker executes background room cleanup sweeps.

If an application relies on Redis for data persistence or distributed locking (e.g., using Redis `SET NX` to serialize financial writes), any Redis downtime or network partition causes application-wide outages and risks data inconsistency.

## Decision

Redis 8 (`redis:8-alpine`) is restricted strictly to coordination tasks. It holds no persistent state and is never required for transactional correctness.

- **Pub/Sub cross-worker fan-out:**
  When a write commits on worker 1, worker 1 publishes the room ID and payload to Redis channel `splitsync:room:{group_id}`. A background listener task on worker 2 receives the message and forwards it to worker 2's local WebSocket connections.
- **Rate limiting counters:**
  Fixed-window counters in Redis track per-IP request rates (`app/api/rate_limit.py`).
- **Distributed sweep leader lock:**
  Workers run a periodic sweep task to delete expired demo rooms (`app/services/rooms.py`). A distributed lock via `SET splitsync:lock:sweep <token> NX PX 60000` ensures only one worker executes the cleanup per interval. The lock is safely released via an atomic Lua script matching the token.
- **Fail-open resilience:**
  If Redis crashes, is unreachable, or encounters an error:
  1. **Writes continue to succeed:** PostgreSQL guarantees ACID transactions and concurrency control.
  2. **Local broadcast fallback:** In [`app/services/realtime.py publish()`](../../app/services/realtime.py), if `redis.publish()` fails, the worker immediately falls back to broadcasting the update to its own local WebSocket connections.
  3. **Rate limiting fails open:** [`check_rate_limit()`](../../app/api/rate_limit.py) returns `allowed = True`.
  4. **Sweep runs anyway:** The sweep is idempotent SQL, so running across multiple workers causes no harm.

## Alternatives considered

- **Using Redis as a distributed lock for payments.** Redundant with PostgreSQL row locks (`SELECT ... FOR UPDATE`), and causes all payments to fail if Redis has an outage.
- **Broadcasting via PostgreSQL `LISTEN / NOTIFY`.** Eliminates Redis, but increases database connection overhead, places broadcast load on the transactional database, and does not provide rate-limiting counters.
- **Failing closed when Redis is unavailable.** Halting financial writes because a cache or messaging bus is down is an operational anti-pattern.

## Consequences

- If Redis crashes, SplitSync continues serving HTTP requests and commits transactions correctly.
- Cross-worker WebSocket updates are temporarily delayed, but clients heal state within 30 seconds via background polling refetches.
- Verified in [`tests/integration/test_redis_down.py`](../../tests/integration/test_redis_down.py), which asserts that writes commit and local WebSockets receive updates when Redis is completely offline.

## When we would revisit

If horizontal scaling moves to hundreds of worker nodes, where Redis Pub/Sub overhead warrants migrating to a dedicated cluster or message bus like Kafka or NATS.
