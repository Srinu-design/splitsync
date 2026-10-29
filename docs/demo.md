# The 4-minute live demo

How to demonstrate SplitSync live in an interview or record the demo video. Every step runs directly in the browser
without special tooling, either on the live production deployment or against a local Docker Compose stack.

- **Live URL:** [https://3-25-14-19.sslip.io/splitsync/](https://3-25-14-19.sslip.io/splitsync/)
- **Local URL:** [http://localhost:8300](http://localhost:8300)

---

## Before you begin

1. **Choose your environment:**
   - **On the live server:** Open [https://3-25-14-19.sslip.io/splitsync/](https://3-25-14-19.sslip.io/splitsync/). The server uses Caddy with automatic Let's Encrypt HTTPS and HTTP/2 multiplexing.
   - **Locally:** Start the development stack:
     ```bash
     docker compose up -d --build --wait
     ```
     and open [http://localhost:8300](http://localhost:8300).
2. **Open two windows side-by-side:**
   - **Left:** Your primary browser window (laptop/desktop).
   - **Right:** A phone browser with the room link (use **Copy link**), or an incognito window placed side-by-side.
3. **Have these ready in browser tabs:**
   - The GitHub repository README;
   - The benchmarks document ([docs/benchmarks.md](benchmarks.md));
   - The live readiness endpoint (`/readyz`).
4. **Practice run:** Click **Start a demo room**, run the race once, and click **Reset room** so you start from a pristine seed.

---

## The script

| Time | On screen | What you say |
|---|---|---|
| **0:00 to 0:30** | Landing page at `https://3-25-14-19.sslip.io/splitsync/` (or `http://localhost:8300`). Click **Start a demo room**. Room opens. | "SplitSync is a real-time expense splitter with debt simplification, append-only history, and exactly-once writes. There is no login—each visitor gets an isolated, seeded room so two people never see each other's state. Notice the status bar: Graph Version 9, Net Balance Sum is exactly zero, and Ledger Drift is zero paise." |
| **0:30 to 0:50** | Click **Copy link** and open the room on the second screen (phone or incognito window). | "I open the same room on my phone. Both screens are connected to SplitSync via WebSockets. When state changes on either device, the server broadcasts full versioned snapshots through Redis Pub/Sub to all connected workers." |
| **0:50 to 1:20** | Scroll to the debt graphs. Point out the left graph (10 IOUs). On the right graph, toggle between **Optimal (3)** and **Greedy (4)**. Expand the step log. | "On the left are the direct pairwise debts as incurred: five friends on a Goa road trip created 10 IOUs where everyone owes someone. On the right is debt simplification. Greedy needs 4 transfers. But our exact bitmask DP solver finds the true mathematical optimum: 3 transfers, a 70% reduction, by finding two disjoint zero-sum subsets." |
| **1:20 to 1:50** | In **Try a race**, pick **Protected** and **10 devices · 10 keys**, then click **Run chaos**. Both screens update live; status log displays results. | "What happens when multiple people record the same UPI payment at the exact same moment? In protected mode, 10 concurrent requests fire. Exactly one succeeds with HTTP 201 Created. The other nine are rejected with HTTP 409 Conflict. A PostgreSQL row lock serializes the writers, and our optimistic graph version check rejects the nine stale requests." |
| **1:50 to 2:15** | In **Try a race**, pick **Same request × 10 · 1 key** and click **Run chaos**. Review the receipts. | "Now a rage-click: a user double-taps on poor connectivity, sending the same request ten times with the same Idempotency-Key. Exactly one payment commits (201 Created), and the other nine receive instant 200 OK replays returning the original receipt ID. No duplicate payment is recorded." |
| **2:15 to 2:45** | On the laptop, click Edit on *Fuel & tolls* (₹9,600). Do not save. On the phone, edit *Fuel & tolls* to ₹9,800 and tap Save. The laptop screen live-updates. Now tap Save on the laptop. | "Here is an edit race. I start editing Fuel & tolls on my laptop. Meanwhile, on my phone, someone else edits it to ₹9,800 and saves. When I try to save my stale edit on the laptop, the server rejects it with 409 EXPENSE_CHANGED and immediately reopens the editor with the latest version. Nothing is silently overwritten." |
| **2:45 to 3:20** | Delete *Scooter rentals*. Click **Undo** in the toast. Open the **History** tab. Type `"din"` in the **Search** box. | "Financial ledgers should never delete data. Deleting Scooter rentals writes a reversal entry. I tap Undo in the toast, and it restores instantly. In the History feed, every version, edit, deletion, and restoration is preserved with keyset pagination. In Search, prefix full-text search matches 'Beach-shack seafood dinner' without needing a heavy GIN index." |
| **3:20 to 3:45** | Click **Reset room**. In **Try a race**, pick **Naive** and **10 devices · 10 keys**, then click **Run chaos**. | "Now let's see why traditional CRUD architectures fail. I switch to Naive mode: no row locks, no version checks, and raw balance overwrites. Ten rows land in the database. Notice the Net Balance Sum badge stays green at ₹0.00! But our Ledger Drift check turns bright red: Aarav and Karthik have drifted by ₹45,000 each from their true balances. Zero-sum checks can't catch lost updates; recomputing from the append-only ledger does." |
| **3:45 to 4:00** | Click **Reset room**. Add an expense of ₹100, split equally among 3 members. Review the resulting shares. | "Finally, money is stored in integer paise, never floating-point. ₹100 split 3 ways yields ₹33.34, ₹33.33, and ₹33.33 using the Hamilton largest-remainder method. The indivisible extra paisa rotates based on the expense ID, so no single person always gets stuck paying it." |

---

## Technical notes for questions during the demo

- **Why does the left graph not change when payments are made?**
  Pairwise accounting cannot absorb simplified cross-payments (e.g. Karthik paying Aarav when Karthik owed Priya). Subtracting settlements pairwise leaves cancelling circular arrows. The left graph shows expenses as incurred; the right graph shows remaining debt to settle.
- **Why does Naive mode keep the sum at zero?**
  Each lost update shifts the payer and payee balances by identical values (+₹5,000 and −₹5,000). Their algebraic sum is still zero. Only comparing cached balances against full ledger recomputations reveals the ₹45,000 discrepancy.
- **Why Caddy and HTTP/2?**
  Desktop browsers limit concurrent HTTP/1.1 connections to 6 per domain. HTTP/2 multiplexing allows 10 concurrent requests to fire simultaneously over a single TLS connection, making the chaos race genuinely concurrent.

