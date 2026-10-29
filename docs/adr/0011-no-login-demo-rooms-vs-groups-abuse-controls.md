# ADR-0011: No login: demo rooms vs groups, caps from a load test, abuse controls

Status: Accepted · Date: 2026-10-29

## Context

Requiring user registration (usernames, passwords, email verification, or OAuth) introduces heavy friction for visitors and interviewers trying out a portfolio application. It also introduces user credential storage liability.

However, operating a public, unauthenticated application exposes infrastructure to abuse: malicious bots creating millions of rooms, storage exhaustion attacks, and noisy tenants degrading database performance.

## Decision

SplitSync requires no login. Rooms are reached by unguessable random URL slugs (64 bits) (`secrets.token_urlsafe(8)`), and separates rooms into two distinct kinds with hard caps:

- **DEMO rooms (`kind = 'DEMO'`):**
  - Created by the **Start a demo room** button.
  - Seeded automatically with the 5-member "Goa Road Trip" scenario.
  - Caps: 12 members, 200 expense versions, 500 settlements.
  - Expiry: Deleted after 24 hours of inactivity by the background sweep task.
  - Chaos panel and Naive mode are enabled.
- **GROUP rooms (`kind = 'GROUP'`):**
  - Created by the **Create group** form for real use, with 2–50 named members.
  - Starts with no expenses; more members can be added later.
  - Caps: 50 members, 10,000 expense versions, 10,000 settlements.
  - Expiry: Retained for 365 days after the last recorded activity.
  - No chaos panel. Naive mode returns `403 NAIVE_MODE_DISABLED` and reset returns `403 RESET_NOT_ALLOWED`.
- **Caps determined from load testing:**
  The caps come from the spec's load test of a full group, and [`tests/load/big_room.py`](../../tests/load/big_room.py) re-measures them: at the caps a group takes 17.6–32 MB on disk and builds its snapshot in 50 ms (this laptop) to 154 ms (spec), once per change.
- **Multi-layer abuse controls:**
  1. **Unguessable URLs:** 8 random bytes (64 bits) as 11 URL-safe characters make enumerating rooms impractical.
  2. **Rate limits:** Per-IP fixed-window limits (10 room creations/hour, 60 writes/min, 120 reads/min, 30 WS handshakes/min) in [`app/api/rate_limit.py`](../../app/api/rate_limit.py).
  3. **Body limit:** Maximum 16 KB request body enforced by [`app/middleware.py`](../../app/middleware.py) (`413 PAYLOAD_TOO_LARGE`).
  4. **Room-count and size guards:** room creation in [`app/services/rooms.py`](../../app/services/rooms.py) refuses new rooms past `MAX_ACTIVE_DEMO_ROOMS` / `MAX_ACTIVE_GROUPS` (`503 ROOMS_FULL`) or when `pg_database_size(current_database())` exceeds `MAX_DB_SIZE_MB`, 2,048 MB on the host (`503 STORAGE_FULL`).

## Alternatives considered

- **User accounts with JWT authentication.** Heavy friction for interviewers; adds password hashing, session management, and credential leak risks without adding core value to the demonstration.
- **Single room type.** Permitting naive race mode and room resets in production groups would risk accidental data loss for real users.
- **Unlimited room growth.** Allows an attacker or run-away script to exhaust disk space on the single EC2 host.

## Consequences

- Zero friction: visitors test the application in one click with a private, fully populated environment.
- Two interviewers testing the app simultaneously never see or mutate each other's data.
- The 2 GB database ceiling and room caps prevent disk exhaustion on the 20 GB host volume.
- Validated in [`tests/integration/test_rooms.py`](../../tests/integration/test_rooms.py).

## When we would revisit

If supporting permanent user accounts with private room dashboards, cross-room contact address books, or paid enterprise team spaces.
