# ADR-0011: No login: demo rooms vs groups, caps from a load test, abuse controls

Status: Accepted · Date: 2026-10-29

## Context

Requiring user registration (usernames, passwords, email verification, or OAuth) introduces heavy friction for visitors and interviewers trying out a portfolio application. It also introduces user credential storage liability.

However, operating a public, unauthenticated application exposes infrastructure to abuse: malicious bots creating millions of rooms, storage exhaustion attacks, and noisy tenants degrading database performance.

## Decision

SplitSync requires no login. Rooms are accessed via unguessable 64-bit cryptographic URL slugs (`secrets.token_urlsafe(8)`), and separates rooms into two distinct kinds with hard caps:

- **DEMO rooms (`kind = 'DEMO'`):**
  - Created via "Try Demo" button for visitors and interviewers.
  - Seeded automatically with the 5-member "Goa Road Trip" scenario.
  - Caps: 12 members, 200 expense versions, 500 settlements.
  - Expiry: Deleted after 24 hours of inactivity by the background sweep task.
  - Chaos panel and Naive mode are enabled.
- **GROUP rooms (`kind = 'GROUP'`):**
  - Created via "Create Group" button for long-running real-world use.
  - Starts empty; members are added on demand.
  - Caps: 50 members, 10,000 expense versions, 10,000 settlements.
  - Expiry: Retained for 365 days after the last recorded activity.
  - Chaos panel, Naive mode, and room reset are strictly disabled (`403 NAIVE_MODE_DISABLED`).
- **Caps determined from load testing:**
  The caps were chosen directly from benchmark results in [`tests/load/big_room.py`](../../tests/load/big_room.py). At maximum capacity (50 members, 10,000 expenses, 10,000 settlements), a room consumes **17.6–32 MB** of disk space and builds its snapshot in **276 ms local** (154 ms reference), preserving responsive performance on a modest server.
- **Multi-layer abuse controls:**
  1. **Unguessable URLs:** 11 URL-safe characters provide 66 bits of entropy, preventing room enumeration.
  2. **Rate limits:** Per-IP sliding limits (10 room creations/hour, 60 writes/min, 120 reads/min, 30 WS handshakes/min) in [`app/api/rate_limit.py`](../../app/api/rate_limit.py).
  3. **Body limit:** Maximum 16 KB request body enforced by [`app/middleware.py`](../../app/middleware.py) (`413 PAYLOAD_TOO_LARGE`).
  4. **Database size guard:** [`app/services/rooms.py check_database_size()`](../../app/services/rooms.py) checks `pg_database_size('splitsync')` and blocks room creation if the database exceeds 500 MB (`MAX_DB_SIZE_MB`).

## Alternatives considered

- **User accounts with JWT authentication.** Heavy friction for interviewers; adds password hashing, session management, and credential leak risks without adding core value to the demonstration.
- **Single room type.** Permitting naive race mode and room resets in production groups would risk accidental data loss for real users.
- **Unlimited room growth.** Allows an attacker or run-away script to exhaust disk space on the single EC2 host.

## Consequences

- Zero friction: visitors test the application in one click with a private, fully populated environment.
- Two interviewers testing the app simultaneously never see or mutate each other's data.
- The 500 MB database ceiling and room caps prevent disk exhaustion on the 20 GB host volume.
- Validated in [`tests/integration/test_rooms.py`](../../tests/integration/test_rooms.py).

## When we would revisit

If supporting permanent user accounts with private room dashboards, cross-room contact address books, or paid enterprise team spaces.
