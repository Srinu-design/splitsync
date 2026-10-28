"""Deploy smoke test: python -m app.smoke [base_url]

Creates one demo room on the running app and checks the seed's verified numbers (SPEC §11):
version 9, 10 pairwise IOUs, greedy 4, optimal 3, zero-sum and zero drift. A release that boots
and passes health checks but computes the ledger wrong fails here. Exit 0 pass, 1 fail.
The room is an ordinary demo room and expires after 24 h idle.
"""

import json
import sys
import urllib.request


def call(base: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def check(state: dict) -> list[str]:
    graph, inv = state["graph"], state["invariants"]
    expected = {
        "graph_version": (state["room"]["graph_version"], 9),
        "pairwise": (graph["pairwise"]["count"], 10),
        "greedy": (graph["greedy"]["count"], 4),
        "optimal": ((graph["optimal"] or {}).get("count"), 3),
        "zero_sum_paise": (inv["zero_sum_paise"], 0),
        "ledger_drift_total_paise": (inv["ledger_drift_total_paise"], 0),
    }
    return [f"{k}: got {got}, want {want}" for k, (got, want) in expected.items() if got != want]


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
    try:
        room = call(base, "/api/rooms", {"kind": "DEMO"})
        problems = check(call(base, f"/api/rooms/{room['slug']}/state"))
    except Exception as exc:  # any HTTP or network error fails the release
        print(f"smoke FAIL: {exc!r}")
        return 1
    if problems:
        print("smoke FAIL: " + "; ".join(problems))
        return 1
    print("smoke pass: demo room 10 IOUs -> 3 transfers (greedy 4), drift 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
