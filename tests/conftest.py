import json
from pathlib import Path

import httpx
import pytest

from sleeper_analyst.config import LeagueConfig, Settings
from sleeper_analyst.sleeper.client import SleeperClient

FIXTURES = Path(__file__).parent / "fixtures"
LEAGUE_ID = "1201891419585785856"
COMPLETED_WEEK = 6
UPCOMING_WEEK = 7

# 2026 NFL bye weeks (mirrors config.example.toml)
BYES = {
    "KC": 5, "CAR": 5,
    "MIA": 6, "CIN": 6, "DET": 6, "MIN": 6,
    "BUF": 7, "LAC": 7, "WAS": 7, "JAX": 7,
    "NYG": 8, "NO": 8, "SF": 8, "HOU": 8,
    "TEN": 9, "PIT": 9,
    "DEN": 10, "PHI": 10, "CHI": 10, "TB": 10,
    "NE": 11, "CLE": 11, "SEA": 11, "GB": 11, "ATL": 11, "LAR": 11,
    "IND": 13, "NYJ": 13, "LV": 13, "BAL": 13,
    "DAL": 14, "ARI": 14,
}


def fixture_json(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


ROUTES = {
    "/v1/state/nfl": "state",
    f"/v1/league/{LEAGUE_ID}": "league",
    f"/v1/league/{LEAGUE_ID}/rosters": "rosters",
    f"/v1/league/{LEAGUE_ID}/users": "users",
    f"/v1/league/{LEAGUE_ID}/matchups/{COMPLETED_WEEK}": f"matchups_{COMPLETED_WEEK}",
    f"/v1/league/{LEAGUE_ID}/matchups/{UPCOMING_WEEK}": f"matchups_{UPCOMING_WEEK}",
    f"/v1/league/{LEAGUE_ID}/transactions/{COMPLETED_WEEK - 1}": f"transactions_{COMPLETED_WEEK - 1}",
    f"/v1/league/{LEAGUE_ID}/transactions/{COMPLETED_WEEK}": f"transactions_{COMPLETED_WEEK}",
    f"/v1/league/{LEAGUE_ID}/transactions/{UPCOMING_WEEK}": f"transactions_{UPCOMING_WEEK}",
    "/v1/players/nfl": "players",
    "/v1/players/nfl/trending/add": "trending_add",
    "/v1/players/nfl/trending/drop": "trending_drop",
}


def make_fixture_transport(calls: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        name = ROUTES.get(request.url.path)
        if name is None:
            return httpx.Response(404, json={"error": f"no fixture for {request.url.path}"})
        return httpx.Response(200, json=fixture_json(name))

    return httpx.MockTransport(handler)


@pytest.fixture
def client():
    with SleeperClient(transport=make_fixture_transport()) as c:
        yield c


@pytest.fixture
def settings(tmp_path):
    my_user_id = fixture_json("rosters")[0]["owner_id"]
    return Settings(
        league=LeagueConfig(league_id=LEAGUE_ID, user_id=my_user_id),
        byes=BYES,
        data_dir=tmp_path,
    )
