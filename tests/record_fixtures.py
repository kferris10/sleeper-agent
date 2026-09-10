"""Record Sleeper API fixtures for the offline test suite.

Usage: uv run python tests/record_fixtures.py [league_id] [completed_week]

Fetches real JSON from the public API and writes it to tests/fixtures/.
The players map is trimmed to players referenced by the league plus a slice of
active free agents so the fixture stays small.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

DEFAULT_LEAGUE = "1201891419585785856"  # "The Legends League" 2025 — public demo league
DEFAULT_COMPLETED_WEEK = 6

FIXTURES = Path(__file__).parent / "fixtures"

PLAYER_FIELDS = (
    "player_id first_name last_name full_name position fantasy_positions team active "
    "status injury_status depth_chart_order depth_chart_position years_exp"
).split()


def main() -> None:
    league_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LEAGUE
    completed = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_COMPLETED_WEEK
    upcoming = completed + 1
    FIXTURES.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(base_url="https://api.sleeper.app/v1", timeout=60)

    def record(path: str, name: str, **params: object):
        data = client.get(path, params=params or None).raise_for_status().json()
        (FIXTURES / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"{name}.json  <-  {path}")
        return data

    record("/state/nfl", "state")
    record(f"/league/{league_id}", "league")
    rosters = record(f"/league/{league_id}/rosters", "rosters")
    record(f"/league/{league_id}/users", "users")
    record(f"/league/{league_id}/matchups/{completed}", f"matchups_{completed}")
    record(f"/league/{league_id}/matchups/{upcoming}", f"matchups_{upcoming}")
    txs = []
    for week in (completed - 1, completed, upcoming):
        txs.extend(record(f"/league/{league_id}/transactions/{week}", f"transactions_{week}"))
    trending_add = record("/players/nfl/trending/add", "trending_add", lookback_hours=48, limit=50)
    trending_drop = record("/players/nfl/trending/drop", "trending_drop", lookback_hours=48, limit=50)

    # Trim the ~5MB player map: keep rostered + transacted + trending players,
    # plus a slice of active free agents per fantasy position.
    players = client.get("/players/nfl").raise_for_status().json()
    keep: set[str] = set()
    for roster in rosters:
        keep.update(roster.get("players") or [])
    for tx in txs:
        keep.update((tx.get("adds") or {}).keys())
        keep.update((tx.get("drops") or {}).keys())
    keep.update(t["player_id"] for t in trending_add + trending_drop)

    per_pos: dict[str, int] = {}
    for pid, p in players.items():
        pos = p.get("position")
        if (
            pid not in keep
            and p.get("active")
            and pos in ("QB", "RB", "WR", "TE", "K", "DEF")
            and per_pos.get(pos, 0) < 30
        ):
            keep.add(pid)
            per_pos[pos] = per_pos.get(pos, 0) + 1

    trimmed = {
        pid: {k: players[pid].get(k) for k in PLAYER_FIELDS if players[pid].get(k) is not None}
        for pid in keep
        if pid in players
    }
    (FIXTURES / "players.json").write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"players.json  <-  /players/nfl (trimmed to {len(trimmed)} of {len(players)})")


if __name__ == "__main__":
    main()
