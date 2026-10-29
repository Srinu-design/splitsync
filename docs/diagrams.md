# SplitSync in diagrams

Fourteen UML diagrams, from who uses SplitSync down to single requests. Each is Mermaid text, so GitHub draws it
here, and `docs/render_diagrams.py` draws the same text to the SVG files in [images/diagrams/](images/diagrams/).
The words behind each diagram are in [design.md](design.md) and the [ADRs](adr/).

| # | Diagram | Kind | Shows |
|---|---|---|---|
| 1 | [Use cases](#1-use-cases) | Use case | Who uses SplitSync, and for what |
| 2 | [Components](#2-components) | Component | The browser, the app's parts, and what talks to what |
| 3 | [Deployment](#3-deployment) | Deployment | Where each part runs, from GitHub to the EC2 host |
| 4 | [Classes](#4-classes) | Class | The core code: pure domain functions, the write helpers, realtime |
| 5 | [Database](#5-database) | Entity–relationship | Every table, key and relationship of the append-only ledger |
| 6 | [An expense's life](#6-an-expenses-life) | State machine | Every state an expense version can be in, and every move between them |
| 7 | [A browser's connection](#7-a-browsers-connection) | State machine | Live, reconnecting with backoff, and how a lost message heals |
| 8 | [Record a payment](#8-record-a-payment) | Sequence | One protected `POST …/settlements`: lock, idempotency, version, balances |
| 9 | [Ten devices, one payment](#9-ten-devices-one-payment) | Sequence | The chaos demo: 1 × 201 and 9 × 409, then a rage-click: 1 × 201 and 9 × 200 |
| 10 | [Two people edit one expense](#10-two-people-edit-one-expense) | Sequence | A reversal plus a new version; the second editor gets 409 `EXPENSE_CHANGED` |
| 11 | [Naive mode loses an update](#11-naive-mode-loses-an-update) | Sequence | Why Σ = 0 stays green while the ledger check turns red |
| 12 | [A write reaches every screen](#12-a-write-reaches-every-screen) | Sequence | Commit, then Redis Pub/Sub to both workers, and what happens without Redis |
| 13 | [From balances to a plan](#13-from-balances-to-a-plan) | Activity | Pairwise IOUs, greedy, and the exact solver up to 15 people |
| 14 | [Deploy and roll back](#14-deploy-and-roll-back) | Sequence | From a push to a smoke-tested release, or back to the old one |

## 1. Use cases

There is no login. A visitor starts a seeded demo room or an empty group, and anyone with the room's link is a
member's device. Demo rooms add the chaos panel and reset; groups never offer them. The operator deploys and
watches.

<!-- diagram: use-cases -->
```mermaid
flowchart LR
  visitor(["Visitor<br/>an interviewer trying the demo"])
  member(["Group member<br/>any device with the room link"])
  operator(["Operator"])

  subgraph demo["Demo rooms only"]
    direction TB
    start("Start a private seeded demo room")
    chaos("Run the chaos panel:<br/>protected, rage-click, naive")
    reset("Reset the demo room")
  end
  subgraph rooms["Rooms"]
    direction TB
    group("Create an empty group")
    share("Share the room link")
    join("Add a member")
  end
  subgraph money["Expenses and payments"]
    direction TB
    add("Add an expense:<br/>equal, shares or exact split")
    edit("Edit an expense")
    del("Delete an expense, then Undo")
    settle("Record a payment between two members")
    unsettle("Delete a recorded payment")
  end
  subgraph see["See and find"]
    direction TB
    view("See balances, IOUs,<br/>greedy and optimal plans")
    live("Get every change live, on every device")
    history("Browse the full history of changes")
    search("Search expenses by words,<br/>payer, amount, date")
  end
  subgraph ops["Operations"]
    direction TB
    deploy("Deploy a release, or roll back")
    watch("Check health, readiness and logs")
  end

  visitor --- demo
  visitor --- see
  member --- rooms
  member --- money
  member --- see
  chaos -. "records payments" .-> settle
  operator --- ops
```

## 2. Components

One FastAPI app runs as two Uvicorn workers. Caddy is the only public entry. PostgreSQL is the single source of
truth; Redis carries only coordination (Pub/Sub, rate-limit counters, the sweep's lock), and everything it does
fails open.

<!-- diagram: components -->
```mermaid
flowchart TB
  browser(["Browser<br/>index.html, room.html, app.js, graph.js<br/>plain JavaScript, no framework"])
  caddy["Caddy<br/>TLS, HTTP/2, /splitsync/ prefix"]
  browser <-->|"HTTPS requests and<br/>WebSocket snapshots"| caddy
  caddy <--> app

  subgraph app["SplitSync app: FastAPI on Uvicorn, 2 workers"]
    direction LR
    mw["Middleware<br/>16 KB body limit,<br/>request id + JSON logs,<br/>CSP and security headers"]
    rl["Rate limits<br/>create_room, write,<br/>read, ws_connect"]
    routes["Routers<br/>rooms, members, expenses,<br/>settlements, state, activity,<br/>search, WebSocket, health"]
    services["Services<br/>writes: lock, idempotency,<br/>version, atomic SQL<br/>reads: state, history, search"]
    domain["Domain, pure functions<br/>money, allocation,<br/>ledger, simplify"]
    rt["Realtime<br/>ConnectionManager,<br/>redis_listener,<br/>publish after commit"]
    sweep["Sweep loop<br/>every 10 min,<br/>expired rooms"]
    mw --> rl --> routes --> services --> domain
    services --> rt
  end

  app -->|"row locks, version checks, unique keys,<br/>append-only rows, DELETE expired rooms"| pg[("PostgreSQL 16<br/>the ledger: source of truth")]
  app <-->|"Pub/Sub fan-out, rate-limit counters,<br/>sweep leader lock: all fail open"| redis[("Redis 8<br/>coordination only")]
```

## 3. Deployment

A push to `main` runs CI; when it is green, GitHub asks the EC2 host to deploy that commit over SSH with a key
that can only run `deploy.sh`. The host builds the image from the commit, migrates, swaps and smoke-tests. Only
Caddy has public ports.

<!-- diagram: deployment -->
```mermaid
flowchart LR
  subgraph github["GitHub"]
    repo["Repository<br/>Srinu-design/splitsync"]
    ci["Actions: ci.yml<br/>ruff, unit + integration tests<br/>on Postgres and Redis, e2e on compose"]
    deploywf["Actions: deploy.yml<br/>after green CI on main"]
    repo --> ci --> deploywf
  end

  internet(["Internet<br/>browsers"])

  subgraph ec2["AWS EC2 portfolio-apps: t3.small, Ubuntu 24.04, Elastic IP"]
    direction TB
    script["/opt/splitsync/src/deploy/deploy.sh<br/>forced SSH command"]
    files["/opt/splitsync<br/>src clone, .env mode 600,<br/>current, previous, deploy-history.log"]
    subgraph net["Docker network portfolio: only Caddy publishes ports"]
      direction TB
      caddy["caddy 2<br/>:80 and :443, Let's Encrypt<br/>for 3-25-14-19.sslip.io"]
      app["splitsync-app<br/>image splitsync:sha, 400 MB limit"]
      migrate["splitsync-migrate<br/>one-off: alembic upgrade head"]
      postgres[("postgres 16 + pgvector<br/>volume, shared with future apps")]
      redis[("redis 8<br/>64 MB, no persistence")]
    end
    script --> files
    caddy -->|"/splitsync/*"| app
    app --> postgres & redis
    migrate --> postgres
  end

  deploywf -->|"ssh deploy key: one commit sha"| script
  script -->|"git checkout, docker build"| app
  internet -->|"HTTPS, HTTP/2"| caddy
```

## 4. Classes

The money and graph logic is pure functions with no I/O, so it is tested without a database. Every write in the
services goes through the same few helpers inside one `write_tx`.

<!-- diagram: classes -->
```mermaid
classDiagram
  direction LR
  class money {
    <<module>>
    +parse_rupees(s) int paise
  }
  class InvalidAmount {
    <<exception>>
  }
  class allocation {
    <<module>>
    +allocate(total, member_ids, weights, rotation) dict
  }
  class ledger {
    <<module>>
    +expense_deltas(paid_by, total, shares) dict
    +settlement_deltas(from, to, amount) dict
    +negate(deltas) dict
    +merge(deltas...) dict
    +integrity_report(rows, bad_splits, orphan_edits) dict
  }
  class simplify {
    <<module>>
    +pairwise_edges(member_ids, owed) list~Edge~
    +greedy(balances) edges, steps
    +exact(balances, max_n=15) edges, groups
    +plans(balances, exact_max) dict
  }
  class shared {
    <<module>>
    +lock_room(conn, slug) room
    +lookup_idempotency(conn, gid, key, hash) WriteResult
    +bump(conn, room, settings) int
    +validate_members(conn, gid, ids)
    +apply_deltas(conn, gid, deltas)
    +payload_hash(parts) str
  }
  class WriteResult {
    <<NamedTuple>>
    +body dict
    +status int
    +headers dict
  }
  class db {
    <<module>>
    +write_tx(engine, operation) T
  }
  class expenses {
    <<service>>
    +create() WriteResult
    +edit() WriteResult
    +delete() WriteResult
    +restore() WriteResult
  }
  class settlements {
    <<service>>
    +create() protected or naive
    +delete() WriteResult
  }
  class state {
    <<service>>
    +build_snapshot(engine, settings, slug) dict
  }
  class realtime {
    <<module>>
    +publish_snapshot_task(app, slug)
    +publish(redis, manager, group_id, payload)
    +redis_listener(redis, manager)
  }
  class ConnectionManager {
    +rooms dict~int, set~
    +ip_sockets dict~str, set~
    +can_connect(group_id, ip) bool
    +connect(group_id, ip, ws)
    +disconnect(group_id, ip, ws)
    +broadcast(group_id, payload)
  }
  class RedisClient {
    +publish(channel, message) bool
    +set_lock(key, token, px) bool
    +release_lock(key, token) bool
    +check_rate_limit(scope, ip, limit, window)
  }
  class ApiError {
    <<exception>>
    +status int
    +code str
    RFC 9457 Problem Details
  }
  class Settings {
    +DEMO_LIMITS Limits
    +GROUP_LIMITS Limits
    +EXACT_MAX_NONZERO int
    +ENABLE_NAIVE_MODE bool
  }

  money ..> InvalidAmount : raises
  expenses ..> money
  expenses ..> allocation
  expenses ..> ledger
  settlements ..> ledger
  expenses ..> shared
  settlements ..> shared
  expenses ..> db
  settlements ..> db
  shared ..> WriteResult : returns
  shared ..> ApiError : raises
  db ..> ApiError : 55P03 to 409 GROUP_BUSY
  state ..> simplify
  state ..> ledger
  realtime ..> state : builds the snapshot
  realtime ..> RedisClient : PUBLISH, PSUBSCRIBE
  realtime ..> ConnectionManager : broadcast to sockets
  shared ..> Settings
```

## 5. Database

Six tables and two views. Rows in `expenses`, `expense_splits`, `settlements` and `reversals` are never updated or
deleted while the room lives; `members.net_balance_paise` is a cache the integrity queries recompute. Every
foreign key inside a room includes `group_id`, so a member of another room cannot be referenced even by a bug.

<!-- diagram: database -->
```mermaid
erDiagram
  groups ||--o{ members : "has"
  groups ||--o{ expenses : "has"
  groups ||--o{ settlements : "has"
  groups ||--o{ reversals : "has"
  members ||--o{ expenses : "paid_by"
  expenses ||--|{ expense_splits : "split into"
  members ||--o{ expense_splits : "owes a share"
  expenses |o--o| expenses : "revision_of (one successor)"
  members ||--o{ settlements : "from_member, to_member"
  expenses |o--o| reversals : "reversed at most once"
  settlements |o--o| reversals : "deleted at most once"

  groups {
    bigint id PK
    text slug UK "8-32 url-safe chars"
    text kind "DEMO or GROUP"
    text name
    bigint graph_version "+1 per write, history clock"
    timestamptz last_active_at
    timestamptz expires_at "24 h demo, 365 d group"
  }
  members {
    bigint id PK
    bigint group_id FK
    bigint version
    text name "unique per room, case-insensitive"
    text color
    bigint net_balance_paise "cache, the ledger is the truth"
  }
  expenses {
    bigint id PK
    bigint group_id FK
    bigint version
    bigint lineage_id FK "id of the first version"
    bigint revision_of FK, UK "previous version"
    bigint paid_by FK
    bigint acted_by FK
    text title
    tsvector title_tsv "generated, simple config"
    date spent_on
    bigint total_paise "1 to 10 crore paise"
    text split_type "EQUAL, SHARES or EXACT"
    text idempotency_key UK "unique per room"
    char payload_hash "SHA-256"
    jsonb response_json "the receipt to replay"
  }
  expense_splits {
    bigint expense_id PK, FK
    bigint member_id PK, FK
    bigint group_id
    int weight "SHARES only"
    bigint share_paise
  }
  settlements {
    bigint id PK
    bigint group_id FK
    bigint version
    bigint from_member FK
    bigint to_member FK
    bigint amount_paise
    text mode "PROTECTED or NAIVE"
    text idempotency_key UK "partial unique: PROTECTED only"
    char payload_hash
    bigint graph_version_before
    jsonb response_json
  }
  reversals {
    bigint id PK
    bigint group_id FK
    bigint version
    bigint expense_id FK, UK
    bigint settlement_id FK, UK
    text reason "EDIT or DELETE"
    text idempotency_key UK "NULL for EDIT"
    jsonb response_json
  }
```

Views: `active_expenses` and `active_settlements` are the rows with no reversal. Every balance, graph and list query
reads them, never the raw tables.

## 6. An expense's life

An expense is a chain of versions sharing one `lineage_id`. Nothing is overwritten: an edit reverses the old
version and inserts a new one, a delete reverses it, and Undo inserts a copy.

<!-- diagram: expense-states -->
```mermaid
stateDiagram-v2
  [*] --> Active : POST expenses<br/>new lineage
  Active --> Replaced : PUT (edit)<br/>EDIT reversal + new version
  Active --> Deleted : DELETE<br/>DELETE reversal, balances negated
  Deleted --> Restored : POST restore (Undo)<br/>copy inserted as a new version
  Replaced --> [*] : its new version is Active
  Restored --> [*] : the copy is Active

  note right of Active
    Shown in balances, graphs and lists,
    read through active_expenses.
  end note
  note left of Replaced
    Kept forever in history.
    UNIQUE (revision_of): one successor.
  end note
  note right of Deleted
    PUT or DELETE on a Replaced or
    Deleted version: 409 EXPENSE_CHANGED,
    naming the current version.
  end note
```

## 7. A browser's connection

Every message is a full snapshot with its `graph_version`, and the page ignores anything not newer. A lost or late
message costs freshness, never correctness: the page re-fetches on every reconnect and every 30 seconds.

<!-- diagram: connection-states -->
```mermaid
stateDiagram-v2
  [*] --> Connecting : room page opens
  Connecting --> Live : socket accepted,<br/>first message is the snapshot
  Connecting --> Closed : 1008 bad Origin, 4404 no room,<br/>1013 over a limit
  Live --> Live : snapshot arrives<br/>render if newer, else drop
  Live --> Reconnecting : socket drops
  Reconnecting --> Connecting : wait 1, 2, 4, 8, 16, then 30 s<br/>plus up to 500 ms jitter
  Live --> Refetch : every 30 s
  Reconnecting --> Refetch : after 2 failed tries,<br/>status shows polling
  Refetch --> Live : GET state with If-None-Match<br/>304, or keep it only if newer
  Closed --> [*]
```

## 8. Record a payment

The protected path of `POST /api/rooms/{slug}/settlements`, in order. Every protected write (expenses, edits,
deletes, restores) follows the same first steps: lock the room row, re-check the key inside the lock, then the
version that write depends on.

<!-- diagram: seq-settle -->
```mermaid
sequenceDiagram
  autonumber
  participant B as Browser
  participant A as API worker
  participant P as PostgreSQL
  participant R as Redis

  B->>A: POST settlements, Idempotency-Key k<br/>from, to, amount, expected_graph_version 9
  A->>A: check the key format (400 if missing)<br/>rate limit "write" (429), parse rupees to paise
  A->>P: BEGIN, lock_timeout 2 s
  A->>P: SELECT groups ... FOR UPDATE
  Note over A,P: one writer per room from here to COMMIT
  A->>P: look up key k in expenses, settlements, reversals
  alt key already used, same body
    P-->>A: stored receipt
    A-->>B: 200 Idempotent-Replayed, the same receipt
  else key used with a different body
    A-->>B: 422 IDEMPOTENCY_KEY_REUSED
  else new key
    alt graph_version is not 9
      A-->>B: 409 STALE_GRAPH_VERSION, current version
    else version matches
      A->>P: SELECT both members ... ORDER BY id FOR UPDATE
      A->>A: amount fits both balances? (else 422 OVER_SETTLEMENT)
      A->>P: UPDATE members SET net_balance_paise = net_balance_paise + delta
      A->>P: UPDATE groups SET graph_version = 10
      A->>P: INSERT settlements, receipt in response_json
      A->>P: COMMIT
      A-->>B: 201 receipt
      A->>R: after commit: PUBLISH the version 10 snapshot
    end
  end
```

## 9. Ten devices, one payment

The chaos panel's two protected runs. Ten different keys race for one payment: the lock lines them up and the
version check rejects the nine that read version 9. Then one key sent ten times: the key check inside the lock
replays the first receipt.

<!-- diagram: seq-chaos -->
```mermaid
sequenceDiagram
  participant D1 as Device 1
  participant D2_10 as Devices 2-10
  participant A as API workers
  participant P as PostgreSQL

  Note over D1,P: Run 1: ten keys, all expecting version 9
  par at the same moment
    D1->>A: POST settle, key k1, expected 9
  and
    D2_10->>A: POST settle, keys k2..k10, expected 9
  end
  A->>P: 10 transactions: SELECT groups ... FOR UPDATE
  Note over P: one holds the row lock, nine wait
  P-->>A: k1 wins: version 9 matches, balances move, version 10
  A-->>D1: 201
  P-->>A: each waiter gets the lock in turn and sees version 10
  A-->>D2_10: 9 x 409 STALE_GRAPH_VERSION

  Note over D1,P: Run 2: one key k sent ten times (a rage-click)
  par
    D1->>A: POST settle, key k
  and
    D2_10->>A: POST settle, key k, same body x 9
  end
  A->>P: first one inside the lock: new key, commit
  A-->>D1: 201 receipt r
  A->>P: the rest, inside the lock: key k found, same payload hash
  A-->>D2_10: 9 x 200 replays of receipt r, no new row
  Note over P: backstop: partial UNIQUE (group_id, idempotency_key) WHERE mode = PROTECTED
```

## 10. Two people edit one expense

You edit the version on your screen. The second editor's target was reversed by the first edit, so the write
stops inside the lock and names the version that replaced it. A change to any other expense never blocks you.

<!-- diagram: seq-edit -->
```mermaid
sequenceDiagram
  autonumber
  participant L as Laptop
  participant Ph as Phone
  participant A as API worker
  participant P as PostgreSQL

  L->>A: GET state: Fuel and tolls is version 42
  Ph->>A: GET state: Fuel and tolls is version 42
  Ph->>A: PUT expenses/42, amount 9,800
  A->>P: lock the room, key is new, load version 42: active
  A->>P: INSERT reversals (expense 42, EDIT)
  A->>P: INSERT expenses 57, revision_of 42, same lineage
  A->>P: balances += new deltas - old deltas, version +1, COMMIT
  A-->>Ph: 200, expense 57 replaces 42
  A-->>L: WebSocket: new snapshot, the list shows 57
  L->>A: PUT expenses/42, amount 9,700, started before the change
  A->>P: lock the room, load version 42: it has an EDIT reversal
  A->>P: find the lineage's current version: 57
  A-->>L: 409 EXPENSE_CHANGED, current_expense_id 57
  L->>L: reopen the editor on version 57
  Note over P: backstops: UNIQUE (revision_of) and reversals.expense_id UNIQUE
```

## 11. Naive mode loses an update

Demo rooms only. Naive mode reads balances without locks, waits 150 ms so requests overlap, and writes absolute
values: the classic lost update. Each write moves the payer and the payee by the same amount, so the sum stays
zero; only recomputing balances from the ledger shows the damage.

<!-- diagram: seq-naive -->
```mermaid
sequenceDiagram
  participant D1 as Device 1
  participant D2 as Device 2
  participant A as API workers
  participant P as PostgreSQL

  par
    D1->>A: POST settle NAIVE, Karthik to Aarav 5,000
  and
    D2->>A: POST settle NAIVE, Karthik to Aarav 5,000
  end
  A->>P: tx 1 reads Karthik -5,000, Aarav +5,000 (no lock)
  A->>P: tx 2 reads Karthik -5,000, Aarav +5,000 (no lock)
  Note over A: both sleep 150 ms
  A->>P: tx 1 SET Karthik = 0, Aarav = 0, INSERT row 1
  A->>P: tx 2 SET Karthik = 0, Aarav = 0, INSERT row 2
  Note over P: two settlement rows, but the cache moved once
  A-->>D1: 201
  A-->>D2: 201
  Note over D1,P: Σ of cached balances is still 0: badge green<br/>ledger says Karthik +5,000, Aarav -5,000: drift badge red
```

## 12. A write reaches every screen

The write commits on one worker, and its snapshot is published to Redis after the commit. Every worker is
subscribed, so each one sends the snapshot to its own sockets. Without Redis, the writing worker sends to its own
sockets and the rest heal on their next 30-second re-fetch.

<!-- diagram: seq-fanout -->
```mermaid
sequenceDiagram
  participant B1 as Browser 1
  participant W1 as Worker 1
  participant P as PostgreSQL
  participant R as Redis
  participant W2 as Worker 2
  participant B2 as Browser 2

  W1->>R: PSUBSCRIBE splitsync:room:*
  W2->>R: PSUBSCRIBE splitsync:room:*
  B1->>W1: POST expenses
  W1->>P: lock, insert, version +1, COMMIT
  W1-->>B1: 201
  W1->>P: background task: build snapshot<br/>REPEATABLE READ, READ ONLY, cached by (room, version)
  alt Redis is up
    W1->>R: PUBLISH splitsync:room:7, snapshot v10
    R-->>W1: pmessage
    R-->>W2: pmessage
    W1-->>B1: snapshot v10 on the socket
    W2-->>B2: snapshot v10 on the socket
  else Redis is down
    W1-->>B1: local broadcast, snapshot v10
    Note over W2,B2: no message: Browser 2 re-fetches state within 30 s
  end
```

## 13. From balances to a plan

Each snapshot turns the room's balances into three answers. Pairwise IOUs come from active expenses only; the two
plans come from net balances. The exact solver is exponential, so above 15 people with a non-zero balance the room
shows the greedy plan, labelled as greedy.

<!-- diagram: plan-activity -->
```mermaid
flowchart TD
  start(["Snapshot for room, version v"]) --> cache{"(room, v) in the<br/>LRU cache?"}
  cache -->|yes| done(["Return the snapshot"])
  cache -->|no| read["Read members, active expenses,<br/>splits, settlements in one<br/>REPEATABLE READ transaction"]
  read --> pair["Pairwise IOUs: each debtor's shares<br/>in what each creditor paid, netted per pair"]
  read --> integ["Integrity: recompute every balance<br/>from the ledger; drift = cached - ledger"]
  read --> bal["Net balances, integer paise, sum 0"]
  bal --> greedy["Greedy: two heaps, largest debtor<br/>pays largest creditor<br/>O(N log N), at most N-1 transfers"]
  bal --> count{"Non-zero balances<br/>15 or fewer?"}
  count -->|yes| exact["Exact: bitmask DP over 2^n subsets<br/>most disjoint zero-sum groups,<br/>greedy inside each group"]
  count -->|no| greedyonly["No exact plan:<br/>recommend greedy, labelled"]
  exact --> rec["Recommend OPTIMAL"]
  pair --> out
  integ --> out
  greedy --> out
  rec --> out
  greedyonly --> out
  out(["Snapshot: graph_version, members, IOUs,<br/>greedy, optimal, invariants"]) --> store["Store in the cache"] --> done
```

On the demo trip: 10 pairwise IOUs, greedy 4 transfers, exact 3 (groups {Karthik, Aarav} and
{Sneha, Priya, Rohan}).

## 14. Deploy and roll back

A push to `main` runs CI. When CI is green, `deploy.yml` sends the commit sha over SSH; the host's key line forces
`deploy.sh --from-ssh`, which accepts only a full sha, `--rollback` or `--status`.

<!-- diagram: seq-deploy -->
```mermaid
sequenceDiagram
  autonumber
  participant Dev as Developer
  participant GH as GitHub Actions
  participant H as EC2: deploy.sh
  participant D as Docker
  participant C as Caddy

  Dev->>GH: git push main
  GH->>GH: ci.yml: ruff, unit + integration on Postgres and Redis,<br/>e2e on docker compose
  GH->>H: deploy.yml: ssh splitsync sha (forced command)
  H->>H: flock, check the sha, git checkout
  H->>D: docker build splitsync:sha
  H->>D: run splitsync-migrate: alembic upgrade head
  H->>D: replace splitsync-app, wait for healthy /readyz
  H->>D: python -m app.smoke in the new container
  alt smoke passes: version 9, 10 IOUs, greedy 4, optimal 3, drift 0
    H->>H: previous = current, current = sha, log it
    H-->>GH: exit 0
    GH->>C: curl https://.../splitsync/readyz
  else health or smoke fails
    H->>D: start the previous image again, smoke test it
    H-->>GH: non-zero exit, the job fails red
  end
```
