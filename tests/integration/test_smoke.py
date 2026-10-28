"""app.smoke.check: passes on a real demo room, names every number that is off."""

import copy

from app.smoke import check


async def test_smoke_check_passes_on_a_fresh_demo_room(client, demo):
    state = (await client.get(f"/api/rooms/{demo['slug']}/state")).json()
    assert check(state) == []


async def test_smoke_check_reports_wrong_numbers(client, demo):
    state = (await client.get(f"/api/rooms/{demo['slug']}/state")).json()
    broken = copy.deepcopy(state)
    broken["graph"]["optimal"]["count"] = 4
    broken["invariants"]["ledger_drift_total_paise"] = 100
    assert check(broken) == [
        "optimal: got 4, want 3",
        "ledger_drift_total_paise: got 100, want 0",
    ]
