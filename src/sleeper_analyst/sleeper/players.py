"""Disk cache for the Sleeper player map with a 24h TTL.

The full /players/nfl payload is ~5MB and Sleeper asks that it be fetched at
most once per day, so we trim it to the fields we use and refuse to refetch
inside the TTL even when forced.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.models import Player

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=24)


class PlayerCache:
    def __init__(self, data_dir: Path, ttl: timedelta = CACHE_TTL) -> None:
        self.cache_file = Path(data_dir) / "cache" / "players.json"
        self.ttl = ttl

    def _read(self) -> tuple[datetime, dict[str, Player]] | None:
        if not self.cache_file.exists():
            return None
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            players = {
                pid: Player.model_validate(p) for pid, p in payload["players"].items()
            }
            return fetched_at, players
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Player cache unreadable (%s); will refetch", exc)
            return None

    def _write(self, players: dict[str, Player]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "players": {
                pid: p.model_dump(exclude_none=True) for pid, p in players.items()
            },
        }
        self.cache_file.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, client: SleeperClient, force: bool = False) -> dict[str, Player]:
        """Return the player map, fetching from the API only if the cache is stale.

        A refetch inside the TTL is refused even with force=True (Sleeper asks
        for at most one fetch per day).
        """
        cached = self._read()
        if cached is not None:
            fetched_at, players = cached
            age = datetime.now(timezone.utc) - fetched_at
            if age < self.ttl:
                if force:
                    logger.warning(
                        "Refusing player-map refetch: cache is %.1fh old (< %dh TTL)",
                        age.total_seconds() / 3600, self.ttl.total_seconds() // 3600,
                    )
                else:
                    logger.info(
                        "Player map served from cache (%.1fh old)", age.total_seconds() / 3600
                    )
                return players
            logger.info("Player cache is %.1fh old; refetching", age.total_seconds() / 3600)

        logger.info("Fetching player map from Sleeper API")
        players = client.get_players()
        self._write(players)
        logger.info("Cached %d players to %s", len(players), self.cache_file)
        return players
