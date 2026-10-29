# ADR-0009: WebSockets send full versioned snapshots; refetch heals

Status: Accepted · Date: 2026-10-29

## Context

Real-time collaboration can be implemented using either incremental delta events (e.g. `{"event": "expense_added", "data": ...}`) or complete state snapshots.

Incremental deltas are fragile over lossy mobile connections. If a smartphone drops a single WebSocket frame while traversing a tunnel, its client-side state permanently diverges from the server. Reconstructing state from missed deltas requires complex sequence numbering, server-side event logs, and catch-up replay protocols.

## Decision

WebSockets transmit complete, versioned room snapshots after every committed write:

- **Full snapshot messages:**
  In [`app/services/state.py build_snapshot()`](../../app/services/state.py), snapshots include room metadata, member balances, the pairwise graph, the simplified plans (greedy and optimal), and ledger invariants:
  ```json
  {
    "type": "snapshot",
    "version": 42,
    "state": { ... }
  }
  ```
- **Monotonic version filtering:**
  Every snapshot carries `version = groups.graph_version`. In [`static/app.js`](../../static/app.js), the browser maintains `currentGraphVersion`. Any incoming snapshot where `version <= currentGraphVersion` is dropped. If network packets arrive out of order, stale snapshots are safely ignored.
- **Self-healing refetch:**
  Clients poll `GET /api/rooms/{slug}/state` every 30 seconds and immediately upon WebSocket reconnection. If a WebSocket message is dropped due to a worker crash or Redis blip, the client heals automatically within 30 seconds.
- **Connection management & security:**
  - `Origin` header validated against `ALLOWED_ORIGINS` before accepting (`close(code=1008)`).
  - Missing or expired room: accept then `close(code=4404)`.
  - Rate limits (30 handshakes/min) and caps (maximum 20 connections per room, 5 per client IP on each worker) close with `code=1013` (Try Again Later).
  - Client message payload limit (1 KB) closes with `code=1009` (Message Too Big).

## Alternatives considered

- **Incremental JSON patches (RFC 6902).** Smaller payloads, but if a single patch is dropped, client state becomes silently corrupted until a hard page reload.
- **Server-Sent Events (SSE).** One-way, but lacks full-duplex ping/pong heartbeat mechanisms and requires separate HTTP requests for client liveness signals.

## Consequences

- Absolute consistency: clients always render a valid, verified database state.
- Simplified frontend: rendering code simply takes the new snapshot and updates the DOM, eliminating client-side state reconciliation algorithms.
- Payload size for a 50-member group with 10,000 expenses is approximately 8–15 KB compressed, easily handled by modern mobile networks.
- Validated in [`tests/integration/test_ws.py`](../../tests/integration/test_ws.py) and [`tests/e2e/test_multiworker_fanout.py`](../../tests/e2e/test_multiworker_fanout.py).

## When we would revisit

If rooms exceed 500 members with thousands of real-time updates per second, where snapshot serialization overhead warrants migrating to compact binary protocols (e.g. Protocol Buffers) or differential CRDT synchronization.
