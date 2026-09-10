"""Thin typed wrapper over the Sleeper public API (https://api.sleeper.app/v1).

No auth required. Retries with exponential backoff + jitter on 429/5xx.
Tests inject an httpx.MockTransport that serves recorded fixtures.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Literal

import httpx

from sleeper_analyst.sleeper.models import (
    League,
    LeagueUser,
    Matchup,
    NflState,
    Player,
    Roster,
    SleeperUser,
    TradedPick,
    Transaction,
    TrendingPlayer,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class SleeperClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SleeperClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response: httpx.Response | None = None
        for attempt in range(self.max_attempts):
            response = self._http.get(path, params=params)
            if response.status_code in RETRY_STATUSES and attempt < self.max_attempts - 1:
                delay = self.backoff_base * (2**attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "GET %s -> %s, retrying in %.2fs (attempt %d/%d)",
                    path, response.status_code, delay, attempt + 1, self.max_attempts,
                )
                time.sleep(delay)
                continue
            break
        assert response is not None
        response.raise_for_status()
        return response.json()

    # -- state -------------------------------------------------------------

    def get_state(self) -> NflState:
        return NflState.model_validate(self._get("/state/nfl"))

    # -- league ------------------------------------------------------------

    def get_league(self, league_id: str) -> League:
        return League.model_validate(self._get(f"/league/{league_id}"))

    def get_rosters(self, league_id: str) -> list[Roster]:
        return [Roster.model_validate(r) for r in self._get(f"/league/{league_id}/rosters")]

    def get_users(self, league_id: str) -> list[LeagueUser]:
        return [LeagueUser.model_validate(u) for u in self._get(f"/league/{league_id}/users")]

    def get_matchups(self, league_id: str, week: int) -> list[Matchup]:
        return [Matchup.model_validate(m) for m in self._get(f"/league/{league_id}/matchups/{week}")]

    def get_transactions(self, league_id: str, week: int) -> list[Transaction]:
        data = self._get(f"/league/{league_id}/transactions/{week}") or []
        return [Transaction.model_validate(t) for t in data]

    def get_traded_picks(self, league_id: str) -> list[TradedPick]:
        return [TradedPick.model_validate(p) for p in self._get(f"/league/{league_id}/traded_picks")]

    # -- players -----------------------------------------------------------

    def get_players(self) -> dict[str, Player]:
        """Full NFL player map (~5MB). Use PlayerCache instead of calling directly."""
        raw = self._get("/players/nfl")
        players: dict[str, Player] = {}
        for player_id, payload in raw.items():
            player = Player.model_validate(payload)
            player.player_id = player.player_id or player_id
            players[player_id] = player
        return players

    def get_trending(
        self,
        kind: Literal["add", "drop"],
        lookback_hours: int = 48,
        limit: int = 50,
    ) -> list[TrendingPlayer]:
        data = self._get(
            f"/players/nfl/trending/{kind}",
            params={"lookback_hours": lookback_hours, "limit": limit},
        )
        return [TrendingPlayer.model_validate(t) for t in data]

    # -- user lookup (for `sleeper-analyst setup`) --------------------------

    def get_user(self, username_or_id: str) -> SleeperUser:
        return SleeperUser.model_validate(self._get(f"/user/{username_or_id}"))

    def get_user_leagues(self, user_id: str, season: str) -> list[League]:
        data = self._get(f"/user/{user_id}/leagues/nfl/{season}") or []
        return [League.model_validate(lg) for lg in data]
