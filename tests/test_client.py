import httpx
import pytest

from sleeper_analyst.sleeper.client import SleeperClient
from conftest import COMPLETED_WEEK, LEAGUE_ID


def test_get_league_parses(client):
    league = client.get_league(LEAGUE_ID)
    assert league.league_id == LEAGUE_ID
    assert league.total_rosters == 12
    assert league.waiver_type == "FAAB"
    assert league.waiver_budget == 100
    assert league.trade_deadline_week == 10
    assert league.playoff_week_start == 15
    assert "BN" in league.roster_positions


def test_get_rosters_parses(client):
    rosters = client.get_rosters(LEAGUE_ID)
    assert len(rosters) == 12
    first = rosters[0]
    assert first.roster_id == 1
    assert first.owner_id
    assert len(first.players) >= len(first.starters)
    assert first.settings.points_for > 0


def test_get_matchups_parses(client):
    matchups = client.get_matchups(LEAGUE_ID, COMPLETED_WEEK)
    assert len(matchups) == 12
    assert all(m.matchup_id is not None for m in matchups)


def test_get_players_sets_ids(client):
    players = client.get_players()
    assert players
    pid, player = next(iter(players.items()))
    assert player.player_id == pid


def test_trending_url_and_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"player_id": "123", "count": 999}])

    with SleeperClient(transport=httpx.MockTransport(handler)) as client:
        trending = client.get_trending("add", lookback_hours=24, limit=10)

    assert trending[0].player_id == "123" and trending[0].count == 999
    request = seen[0]
    assert request.url.path == "/v1/players/nfl/trending/add"
    assert request.url.params["lookback_hours"] == "24"
    assert request.url.params["limit"] == "10"


def test_retries_on_429_then_succeeds():
    statuses = [429, 500, 200]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[len(calls)]
        calls.append(status)
        return httpx.Response(status, json={"week": 7, "season": "2025", "season_type": "regular"})

    with SleeperClient(transport=httpx.MockTransport(handler), backoff_base=0.001) as client:
        state = client.get_state()

    assert state.week == 7
    assert calls == [429, 500, 200]


def test_gives_up_after_max_attempts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={})

    with SleeperClient(transport=httpx.MockTransport(handler), backoff_base=0.001, max_attempts=3) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_state()

    assert len(calls) == 3


def test_4xx_raises_without_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={})

    with SleeperClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_league("nope")

    assert len(calls) == 1
