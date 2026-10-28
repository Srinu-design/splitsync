# SplitSync — Master Guide

Oct 27, 2026 · @Srinu

## What it is

SplitSync is a real-time group expense splitter. It turns a group's tangle of IOUs into the fewest transfers, records every write exactly once even when several phones submit at the same moment, and keeps every change — edits and deletes included — as history you can undo.

**30-second pitch (say it like this):**

> "On a trip or in a flat, a handful of expenses turns into a web of IOUs. SplitSync collapses it into the minimum number of transfers: a two-heap greedy for speed, plus an exact bitmask-DP solver that shows when greedy isn't optimal.
>
> Money is integer paise, so balances always sum to exactly zero, and the ledger is append-only: an edit or delete adds a reversal entry instead of overwriting, so history is complete and any delete can be undone. The hard part is concurrency. When two people record the same UPI payment or edit the same expense at once, exactly one wins — Postgres row locks, version checks and idempotency keys enforce it — and every open phone gets the new state over WebSockets."

**One-line header:** Real-time expense splitter with debt simplification, append-only edit history and exactly-once writes.

**What it is not:** it does not move money. Users pay each other over UPI; SplitSync records those payments and keeps the books correct.

## The problems it solves

Six real failure modes, each fixed and checked in code against PostgreSQL 16.

| Problem | What breaks in a naive app | SplitSync's fix |
| --- | --- | --- |
| IOU tangle | 8 trip expenses among 5 friends leave every pair owing each other: 10 pairwise IOUs | Collapse net balances into at most N−1 transfers; an exact solver finds the true minimum (3 for the demo trip) |
| Double-recorded payment | Both people in one UPI payment record it, or a double-tap on 3G sends it twice | Idempotency keys + a graph-version check + a row lock: one record, the rest rejected or replayed |
| Lost update | Two writers read the same balance and overwrite each other | One lock per group serializes writers; balances change with atomic SQL; a ledger check catches drift |
| Paise drift | ₹100 ÷ 3 as floats loses ₹0.01, so balances stop summing to zero | Integer paise + largest-remainder split: ₹33.34 / ₹33.33 / ₹33.33 |
| Fixing a mistake | Editing or deleting overwrites the row: the old values are gone and a wrong delete can't be undone | Append-only reversal entries: every version stays, the history shows each change, and a delete undoes in one tap |
| Two edits at once | Both apply, or the later one silently overwrites a version its author never saw | You edit the version on your screen; whoever comes second gets 409 with the new version. 10 simultaneous edits → 1 applied, 9 rejected |

**Say it precisely.** SplitSync records payments; it never moves money. So the bug is "the books count one payment twice", not "Rohan gets paid twice".

Two people adding *different* expenses at once is not a conflict. Both must succeed, and they do (10 concurrent adds, 10 commits, zero drift).

## Architecture at a glance

&#91;embedded content: SplitSync architecture · write path and deploy path\]

A write lands on one worker, commits in Postgres, and is published to Redis, which delivers it to every worker so each pushes the new snapshot to its own sockets. Deploys never build on the 1 GB host: CI builds the image, EC2 only pulls it.

## Design decisions you must defend

Sixteen decisions carry the project. Know the *why* of each; interviewers probe these, not the CRUD.

1. **Money is integer paise.** Amounts arrive as strings ("100.50"), are parsed with `Decimal`, and stored as `BIGINT` paise. No float touches money, so "balances sum to zero" holds exactly.
2. **Largest-remainder splits.** Each share is floor(total × weight ÷ Σweights); leftover paise go to the largest fractional remainders. Ties rotate by expense id, so nobody always pays the extra paisa. Every share lands within 1 paisa of its exact proportion (property-tested on 2,000 random splits).
3. **The ledger is the truth; balances are a cache.** Expenses, splits, settlements and reversals are append-only facts. Each member's `net_balance_paise` updates in the same transaction for fast reads, and integrity queries recompute it from the ledger.
4. **Greedy for speed, exact for truth.** The two-heap greedy runs in O(N log N) with at most N−1 transfers, but it is not optimal: on the demo trip greedy needs 4 transfers and the exact bitmask DP (O(2ⁿ·n)) finds 3. Exact runs when at most 15 people have a non-zero balance — measured 3.7 ms at 12 and 35.6 ms at 15 — and above that the plan is greedy and labelled so.
5. **The "before" graph is expenses-only.** Applying settlements pairwise breaks it: paying the optimal plan still leaves 10 arrows that cancel per person (verified). So the left graph shows IOUs as incurred, from live expenses; the right graph shows what's left, from net balances.
6. **Postgres is the correctness layer.** Every write locks its group row (`SELECT … FOR UPDATE`), re-checks the idempotency key inside that lock, checks the relevant version, then changes balances with atomic SQL. Unique constraints back each rule even if the code has a bug.
7. **Lock and version check do different jobs.** The lock stops *simultaneous* writes; the version check stops *stale* writes made from an old screen. A lock alone would still accept the same payment recorded by the other person a second later.
8. **Redis is coordination, never correctness.** Pub/Sub fans WebSocket updates out across Uvicorn workers, counters rate-limit the public site, and a token-checked `SET NX PX` lock lets exactly one worker run the room-expiry sweep. If Redis dies, all three fail open and Postgres still guarantees exactly-once.
9. **WebSockets carry versioned snapshots.** Each message is the full state plus `graph_version`; clients drop anything older than what they have. They reconnect with backoff and re-fetch on reconnect and every 30 s, so a lost message costs freshness, never correctness.
10. **Two kinds of room.** A demo room is seeded, small (12 members, 200 expense versions, 500 settlements), deleted after 24 h idle, and has the chaos panel and reset. A group starts empty, holds 50 members, 10,000 expense versions and 10,000 settlements, is kept for 365 days after its last use, and never offers naive mode or reset.
11. **Standard HTTP semantics.** `PUT` to edit, `DELETE` to delete, `Idempotency-Key` with the IETF draft's codes (400 missing, 422 reused with a different body, 409 conflict), and RFC 9457 Problem Details for every error.
12. **Edits and deletes are reversal entries.** An edit writes, in one transaction, a reversal of the old version and a new version linked to it; a delete writes a reversal; a restore writes a copy. Nothing is overwritten, so the history and undo come for free and the integrity check still sees every rupee.
13. **Edits are checked per expense.** You edit the version id on your screen. If someone changed that expense first, you get 409 `EXPENSE_CHANGED` naming the current version — but an unrelated change elsewhere in the room never blocks you. `UNIQUE (revision_of)` and a unique reversal target are the database backstop.
14. **`graph_version` is the history clock.** Every write bumps it once and stamps its rows with it, so the history feed sorts by version and pages with a keyset cursor — never `OFFSET` — reading at most 4 × (limit + 1) rows per page.
15. **Search is scoped, simple and prefix-based.** Postgres full-text search with the `simple` config, each word a prefix, within one room. No GIN index: the room's btree index narrows to at most 10,000 rows first, and searches measured 10–42 ms at that size.
16. **Limits come from a load test, not guesses.** A full group (50 members, 9,900 expense versions, 9,900 settlements) built its snapshot in a median 154 ms, wrote in 22.5 ms and paged history in 9 ms. A database-size guard, not the room count, protects the disk.

## Verified demo numbers

The seeded "Goa Road Trip" yields 10 pairwise IOUs, 4 greedy transfers and 3 optimal ones. Every number below came from running the reference code and the spec's SQL on PostgreSQL 16 with 10 separate connections, not from estimates.

| Expense | Paid by | Amount | Split |
| --- | --- | --- | --- |
| Villa booking (2 nights) | Aarav | ₹10,200 | Equal, all 5 |
| Fuel & tolls | Priya | ₹9,600 | Equal, all 5 |
| Beach-shack seafood dinner | Rohan | ₹6,100 | Equal, all 5 |
| Scooter rentals | Sneha | ₹3,300 | Equal, all 5 |
| Dudhsagar jeep safari | Karthik | ₹5,800 | Equal, all 5 |
| Parasailing | Aarav | ₹4,500 | Equal: Aarav, Sneha, Karthik |
| Cafe brunch | Priya | ₹2,400 | Shares 1 : 1 : 2 — Priya, Rohan, Karthik |
| Club night | Rohan | ₹5,000 | Exact: Aarav 1,200 · Priya 900 · Rohan 1,000 · Sneha 800 · Karthik 1,100 |

Total spend ₹46,900. Net balances: Aarav +₹5,000 · Priya +₹3,500 · Rohan +₹2,500 · Sneha −₹6,000 · Karthik −₹5,000 (sum ₹0).

| Check | Result |
| --- | --- |
| Pairwise IOUs (A↔B netted) | 10 — every pair owes someone |
| Greedy two-heap | 4: Sneha→Aarav ₹5,000 · Karthik→Priya ₹3,500 · Karthik→Rohan ₹1,500 · Sneha→Rohan ₹1,000 |
| Exact bitmask DP | 3: Karthik→Aarav ₹5,000 · Sneha→Priya ₹3,500 · Sneha→Rohan ₹2,500 (zero-sum groups {Karthik, Aarav} and {Sneha, Priya, Rohan}) |
| Reduction | 10 → 3 (70%) optimal; 10 → 4 (60%) greedy |
| ₹100 split 3 ways | 3,334 / 3,333 / 3,333 paise; the extra paisa rotates by expense id |
| Protected: 10 devices record Karthik→Aarav ₹5,000 | 1 × 201, 9 × 409 `STALE_GRAPH_VERSION`; version +1; drift ₹0 |
| Protected: same request × 10 (rage-click) | 1 × 201, 9 × 200 replays of the same receipt; 1 row |
| Same key, different amount | 422 `IDEMPOTENCY_KEY_REUSED` |
| Naive: 10 devices | 10 rows; the sum stays ₹0 (badge green), but Aarav and Karthik each drift ₹45,000 from the ledger |
| 10 different expenses at once | 10 × 201; version +10; drift ₹0 |
| Edit Fuel & tolls ₹9,600 → ₹9,800 | Balances become Aarav +₹4,960 · Priya +₹3,660 · Rohan +₹2,460 · Sneha −₹6,040 · Karthik −₹5,040; greedy and optimal both 4 now |
| 10 edits of one version at once | 1 × 200, 9 × 409 `EXPENSE_CHANGED` naming the new version |
| Same edit × 10 (one key) | 1 × 200, 9 × 200 replays; one new version |
| 5 deletes + 5 edits of one version at once | 1 succeeds, 9 × 409 |
| Delete, then Undo | Balances return exactly; a shares split keeps its 1 : 1 : 2 weights |
| 10 deletes of one settlement at once | 1 × 200, 9 × 409 `SETTLEMENT_ALREADY_DELETED` |
| Member id from another room | 422 from the API; the composite foreign key also rejects it in the DB |

**Big-room load test** (dev container, 2 vCPUs, PostgreSQL 16; one group at its caps — 50 members, 9,900 expense versions with 59,566 splits, 9,900 settlements, 1,000 reversals):

| Measure | Median |
| --- | --- |
| Snapshot build (pairwise 49 ms + integrity 100 ms + rest) | 154 ms, once per change |
| History page of 50 | 9 ms |
| Search, five query shapes | 10–42 ms |
| Add an expense (incl. opening a connection) | 22.5 ms |
| Exact solver at 12 / 13 / 14 / 15 / 16 non-zero balances | 3.7 / 12.8 / 22.2 / 35.6 / 75.4 ms |
| Database size with that room | 17.6 MB |

## Resume bullets

Use these once your build passes the spec's tests; every number is reproducible from the seed, the test suite or the load check.

**SplitSync — Real-time expense splitter with debt simplification and edit history** | Python, FastAPI, PostgreSQL, Redis, WebSockets, Docker, GitHub Actions, AWS EC2

- Built a real-time group expense splitter that collapses pairwise IOUs into the fewest transfers using a two-heap greedy (O(N log N), ≤ N−1 transfers) and an exact bitmask-DP solver; on the demo trip, 10 IOUs reduce to 3 transfers where greedy needs 4.
- Made every write exactly-once under concurrency with PostgreSQL row locks, optimistic versioning and Idempotency-Key replay backed by unique constraints; 10 simultaneous settlement or edit requests yield 1 commit and 9 rejections in integration tests.
- Designed an append-only ledger where edits and deletes are reversal entries, giving full history, one-tap undo, a keyset-paginated activity feed and prefix full-text search; load-tested at 50 members and 10,000 expense versions.
- Deployed on AWS EC2 via GitHub Actions → GHCR → Docker Compose behind Nginx (TLS, HTTP/2), with Redis Pub/Sub fanning WebSocket updates across Uvicorn workers.

Tight on space? Keep the first three.

**Never write:** "reduces IOUs by 73%" (it compared against an un-netted graph), "prevents double payments" (it doesn't move money), "Redis locks prevent race conditions" (Postgres does), or "event-sourced" without qualifying it (it's an append-only ledger plus a cached balance — a light form; say exactly that if asked).

## Live demo script (4 minutes)

Nine steps, in this order; the last is optional. Run the naive step only on a freshly reset room so the numbers match.

1. **Click "Start demo".** Say: "No login — each visitor gets a private seeded room, so two interviewers never see each other's broken state." Point at the status bar: graph version, Σ = ₹0.00, ledger drift ₹0, WebSocket connected.
2. **Copy the room link and open it on your phone.** Say: "Both screens stay in sync over WebSockets, even across server workers."
3. **Show the two graphs.** Left: 10 red IOUs as incurred. Right: toggle Greedy (4) and Optimal (3), and open the step log. Say: "Greedy is O(N log N) and at most N−1 transfers, but not optimal. The exact solver splits balances into the most zero-sum groups."
4. **Chaos, protected: "10 devices record Karthik → Aarav ₹5,000".** The log shows 1 × 201 and 9 × 409, and both screens update once. Say: "The row lock serializes writers; the version check rejects the nine stale ones."
5. **Chaos, rage-click: "Same request × 10".** The log shows 1 × 201 and 9 × 200 replays with the same receipt id. Say: "Same Idempotency-Key, so retries get the original receipt."
6. **Edit conflict.** On the laptop, open Edit on "Fuel & tolls" and change the amount, but don't save. On the phone, edit the same expense to ₹9,800 and save; the laptop's list updates. Now save on the laptop: it refuses and reopens the editor on the new version. Say: "You edit the version you saw. Whoever comes second gets a 409 that names the current version — nothing is silently overwritten."
7. **Delete, Undo, History, Search.** Delete "Scooter rentals", tap Undo in the toast, then open History: it lists the edit with ₹9,600 → ₹9,800, the delete and the restore. Type "din" in Search: the seafood dinner appears. Say: "The ledger is append-only. An edit or delete is a reversal entry, so history and undo come free."
8. **Reset the room, switch to Naive, run "10 devices" again.** Ten rows land. The Σ badge stays green, but the ledger-drift badge turns red: Aarav and Karthik each off by ₹45,000. Say: "Zero-sum can't catch lost updates; recomputing from the ledger can."
9. **Optional: reset, then "Add ₹100 split 3 ways".** The row shows ₹33.34 / ₹33.33 / ₹33.33 and Σ stays ₹0.00. Say: "Integer paise; the leftover paisa rotates so nobody always pays it."

## Interview Q&A

Twenty-eight questions in rough order of how often they come up. Answers are short on purpose; say them in your own words.

**1. Why integer paise, not floats or Decimal?** Floats can't hold 0.1 exactly, so ₹33.33 × 3 = ₹99.99. Decimal would work, but integer paise are exact, map to `BIGINT`, and make "balances sum to zero" a plain integer check. Decimal is used once, to parse the input string.

**2. How do you split ₹100 three ways?** 10,000 ÷ 3 gives 3,333 each with 1 paisa left over. The leftover goes to the largest fractional remainder, ties broken by a rotation seeded with the expense id: 3,334 / 3,333 / 3,333.

**3. Is your greedy optimal?** No. The minimum is N − (the most disjoint zero-sum groups), and even deciding whether one zero-sum subgroup exists is subset-sum, so the problem is NP-hard. Greedy guarantees at most N−1 transfers because each step zeroes at least one person. Our own seed shows the gap: greedy 4, optimal 3.

**4. So how do you get the optimum?** Bitmask DP: dp\[mask\] = max over i in mask of dp\[mask without i\], plus 1 if the mask sums to zero; the answer is n − dp\[full\], in O(2ⁿ·n). It's LeetCode 465, "Optimal Account Balancing". It runs when at most 15 people have a non-zero balance — 35.6 ms at 15 and 75.4 ms at 16 in CPython — and above that the app shows the greedy plan, labelled as greedy.

**5. Why heaps?** Each step needs the current largest creditor and debtor after partial payments. A heap returns them in O(log N); a one-time sort goes stale after the first partial payment.

**6. What actually races in this app?** Three things: the same request sent twice (double-tap, retry), the same UPI payment recorded by both people, and two writers overwriting a cached balance. Two different expenses at once is not a race; both must succeed.

**7. Why both a row lock and a version check?** The lock stops two writers being inside at once (pessimistic). The version check rejects a write made from a screen that is already out of date (optimistic). The second person recording the same payment arrives after the first commits, so only the version check stops them.

**8. Why re-check the idempotency key inside the lock?** A retry can pass a pre-lock check while the original is still committing. Checked after `FOR UPDATE`, it sees the committed row and replays the receipt: 9 × 200 in the test. The partial `UNIQUE` index is the backstop.

**9. Why not `asyncio.Lock()`?** It lives inside one process. With 2 Uvicorn workers it protects nothing across them. Postgres row locks work across processes and machines.

**10. Then what is Redis for?** What Postgres doesn't do well across processes: Pub/Sub so a write on worker 1 reaches sockets on worker 2, rate-limit counters, and a leader lock so one worker runs the expiry sweep. All three fail open.

**11. Why a Lua script to release that lock?** The lock's value is a random token. If my lock expired and another worker took it, a plain `DEL` would delete theirs. Lua makes "compare token, then delete" one atomic step.

**12. What if Redis goes down?** Writes stay exactly-once because Postgres enforces it. Cross-worker live updates pause, and clients heal by re-fetching every 30 s and on reconnect. Rate limits switch off and the sweep runs anyway (it's idempotent).

**13. What if a WebSocket message is lost or arrives out of order?** Every message is a full snapshot with `graph_version`, and clients ignore anything not newer. The 30-second re-fetch heals a lost message.

**14. Can it deadlock?** Protected writers lock the group row first, then member rows in ascending id; the naive path touches only member rows (ascending) inside its transaction. One fixed order means no lock cycles. Postgres would still detect a deadlock (40P01); the service retries 40P01 and 40001 up to 3 times with jitter.

**15. Why doesn't the left graph change when someone settles?** Pairwise accounting can't absorb a payment between two people who didn't owe each other directly. Paying the optimal plan would leave 10 arrows that cancel per person (verified). So the left graph shows IOUs as incurred and the right shows what's left.

**16. Why doesn't the Σ = 0 badge catch the naive bug?** Each lost update shifts payer and payee by the same amount, so the sum stays zero. Only recomputing from the ledger exposes it: ₹45,000 of drift each in the demo.

**17. Publishing after commit isn't atomic. What if the process dies in between?** That message is lost, and clients heal on the next re-fetch. At scale I'd use a transactional outbox, or Postgres `LISTEN/NOTIFY`, which delivers only on commit.

**18. How does it run on a 1 GB instance?** CI builds and pushes images to GHCR, so EC2 only pulls. A 2 GB swap file, memory caps on Postgres and Redis, and migrations as a one-off job before the app restarts.

**19. Why HTTP/2 in Nginx?** Chrome opens at most 6 HTTP/1.1 connections per host, so 10 "simultaneous" requests would queue. HTTP/2 multiplexes them on one connection, which keeps the chaos demo genuinely concurrent.

**20. How do you stop abuse of a public, no-login site?** Rooms at unguessable URLs, per-IP rate limits, hard caps per room kind (a demo room: 12 members and 200 expense versions; a group: 50 and 10,000), a database-size guard, idle expiry, `textContent`-only rendering against XSS, and an Origin check on the WebSocket.

**21. How do you edit an append-only ledger?** An edit writes two rows in one transaction: a reversal of the old version and a new version linked to it by `revision_of`. Balances change by "old negated plus new". Nothing is overwritten, so the history and the integrity check still see everything.

**22. Why not just `UPDATE` the row?** You'd lose what it said before, the history couldn't show "₹9,600 → ₹9,800", undo would need a separate audit table, and two racing updates would silently lose one. Appending makes every change explicit.

**23. What if two people edit the same expense at once?** Each edit names the version it started from. Inside the room lock, the second finds that version already reversed and gets 409 `EXPENSE_CHANGED` with the new version's id, and the UI reopens the editor on it. In the test, 10 simultaneous edits gave 1 applied and 9 rejected; `UNIQUE (revision_of)` is the database backstop.

**24. Why don't edits use the room-wide version check, like settlements?** That check would reject an edit just because someone added an unrelated expense. A settlement depends on every balance, so it needs the room-wide check; an edit depends only on its own expense, so the check is per expense.

**25. How does the history feed page across four tables?** Every row stores the `graph_version` that created it. Each table's part of the query is sorted and limited by (version, id) with the same keyset cursor, then the union is sorted and cut — at most 4 × (limit + 1) rows per page, about 9 ms on the full-size test room. `OFFSET` would rescan every row before the page.

**26. How does search work, and why no GIN index?** Each word becomes a prefix term in a `simple` tsquery, ANDed, so "seafood din" finds the dinner. Every search is scoped to one room, which its btree index narrows to at most 10,000 rows first: 10–42 ms at that size. A GIN index over all rooms would match the words in everyone's rooms first.

**27. What happens with 50 members?** The exact solver is exponential, so above 15 non-zero balances the plan falls back to greedy — still at most N−1 transfers — and says so, and the graphs switch from circles to lists. A full group's snapshot took 154 ms to build, once per change.

**28. How would you show the group as it was last Tuesday?** Every ledger row carries the version that created it and every reversal the version that undid it, so the state at version v is the rows with version ≤ v that weren't reversed by then. Same queries, one extra filter.

## Know-your-code checklist

The agent writes the code; you have to own it. Tick each item once you can explain it without notes.

- [ ] Trace `allocate()` on paper for ₹100 ÷ 3 and for shares 1 : 1 : 2.
- [ ] Draw the two heaps for the seed and run greedy by hand (4 steps).
- [ ] Explain the bitmask-DP recurrence and why n − dp\[full\] is the minimum.
- [ ] Walk the protected settlement transaction: lock → idempotency → version → validate → update → bump version → insert.
- [ ] Say what each status means here: 201, 200 replay, 400, 409, 422, 429.
- [ ] Explain why the naive path loses updates and why Σ = 0 stays green.
- [ ] Explain the composite foreign keys and why they are `DEFERRABLE`.
- [ ] Draw how a write on worker 1 reaches a socket on worker 2.
- [ ] Read `app.js` once: connect, reconnect backoff, version check, `textContent` rendering.
- [ ] Explain the Lua release script and the sweep's leader lock.
- [ ] Run the tests locally and explain one concurrency test line by line.
- [ ] Walk the pipeline from `git push` to the new container running on EC2.

For the edit-history features:

- [ ] Say what an edit, a delete and a restore each insert, and which balance deltas each applies.
- [ ] Trace two people editing the same expense at once and point to the line where the second fails.
- [ ] Explain the history query: why each part has its own `ORDER BY` and `LIMIT`, and how the cursor moves.
- [ ] Explain the search query: tokens, prefix matching, and what `include_deleted` adds.
- [ ] Explain the switch from exact to greedy at 15 and the measurements behind it.

## What changed from the old spec

Thirteen fixes; the first five would have failed a senior interviewer's follow-up.

| Old spec | Problem | Now |
| --- | --- | --- |
| 11 raw arrows → 3 ("73%") | 11 > C(5,2) = 10, so pairs weren't netted; the baseline was inflated | Seed engineered and verified: 10 netted IOUs → 3 optimal, 4 greedy |
| WebSocket broadcaster in memory, 2 workers | Sockets on the other worker never update | Redis Pub/Sub fan-out, versioned snapshots, periodic re-fetch |
| Expenses are "just INSERTs" | They also update cached balances and the version: lost updates | Expenses take the same group lock, use atomic SQL, and are idempotent |
| Idempotency index not `UNIQUE`, checked before the lock | Duplicates possible; a retry got 409 instead of its receipt | Partial `UNIQUE` index; re-check inside the lock; 9 × 200 replays |
| Naive mode "corrupts balances" | Σ stays ₹0, so the badge never turns red | Ledger-integrity badge shows ₹45,000 drift each |
| Greedy only | Greedy isn't optimal; "loops infinitely" was false | Greedy plus an exact bitmask DP, both shown |
| Redis `SET NX` lock around settlements | Redundant with `FOR UPDATE`; a Redis outage blocked every settle | Postgres owns correctness; Redis does Pub/Sub, rate limits, a sweep leader lock, all fail-open |
| Raw graph subtracts settlements | Leaves phantom arrows after paying | Left graph = expenses only; right graph = net balances |
| One shared room, opened by a GET | Interviewers collide; link previews create state | Per-visitor rooms via POST, 24 h expiry |
| Seeding in app startup with 2 workers | Both workers seed: duplicates | Alembic migration job; rooms seeded on demand |
| Tailwind Play CDN | It warns in the console that it isn't for production | Plain CSS, no build step |
| 409 when a key is reused with a new body | Not the standard code | IETF draft codes (400 / 409 / 422) and RFC 9457 errors |
| "Rohan gets paid twice" | The app never moves money | "The books count one payment twice" |

### Second revision: editing, history, bigger groups, search

Six more changes, each built and tested against PostgreSQL 16 before it went into the spec.

| Before | Problem | Now |
| --- | --- | --- |
| No edit or delete | A typo in an expense was permanent | Edit, delete and undo as append-only reversal entries |
| Newest 50 rows only; every room expired after 24 h | No real history | A paginated history of every change; groups kept while in use |
| 12 members, 200 expenses, 500 settlements | Too small for a flat or a long trip | Groups: 50 members, 10,000 expense versions, 10,000 settlements, set from a load test |
| Exact solver only, hence the 12-member cap | Exponential cost | Exact up to 15 non-zero balances; greedy, labelled, above |
| No search | Old expenses were hard to find | Prefix full-text search with payer, member, amount and date filters |
| One room type | Real groups would inherit demo-only tools | Demo rooms and groups; naive mode and reset only in demos |
