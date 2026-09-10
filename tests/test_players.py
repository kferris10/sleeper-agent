import json
from datetime import datetime, timedelta, timezone

from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.players import PlayerCache
from conftest import make_fixture_transport


def make_client(calls):
    return SleeperClient(transport=make_fixture_transport(calls))


def _age_cache(cache: PlayerCache, hours: float) -> None:
    payload = json.loads(cache.cache_file.read_text(encoding="utf-8"))
    payload["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    cache.cache_file.write_text(json.dumps(payload), encoding="utf-8")


def test_fetches_once_then_serves_from_cache(tmp_path):
    cache = PlayerCache(tmp_path)
    calls: list[str] = []
    with make_client(calls) as client:
        first = cache.get(client)
        second = cache.get(client)

    assert first and second.keys() == first.keys()
    assert calls.count("/v1/players/nfl") == 1
    assert cache.cache_file.exists()


def test_stale_cache_is_refetched(tmp_path):
    cache = PlayerCache(tmp_path)
    calls: list[str] = []
    with make_client(calls) as client:
        cache.get(client)
        _age_cache(cache, hours=25)
        cache.get(client)

    assert calls.count("/v1/players/nfl") == 2


def test_force_refetch_refused_inside_ttl(tmp_path):
    cache = PlayerCache(tmp_path)
    calls: list[str] = []
    with make_client(calls) as client:
        cache.get(client)
        players = cache.get(client, force=True)

    assert calls.count("/v1/players/nfl") == 1
    assert players


def test_corrupt_cache_triggers_refetch(tmp_path):
    cache = PlayerCache(tmp_path)
    calls: list[str] = []
    with make_client(calls) as client:
        cache.get(client)
        cache.cache_file.write_text("not json", encoding="utf-8")
        players = cache.get(client)

    assert calls.count("/v1/players/nfl") == 2
    assert players
