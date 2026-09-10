"""Pydantic models for the Sleeper API and the collected WeeklyContext."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# API-side models (parsed from api.sleeper.app responses; extra keys ignored)
# ---------------------------------------------------------------------------


class SleeperModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NflState(SleeperModel):
    week: int
    season: str
    season_type: str  # "pre" | "regular" | "post"
    display_week: int | None = None


class League(SleeperModel):
    league_id: str
    name: str
    season: str
    status: str | None = None
    total_rosters: int
    roster_positions: list[str]
    scoring_settings: dict[str, float] = Field(default_factory=dict)
    settings: dict[str, float] = Field(default_factory=dict)

    @property
    def waiver_type(self) -> str:
        # Sleeper: settings.waiver_type 2 = FAAB auction, otherwise rolling/reversal priority
        return "FAAB" if int(self.settings.get("waiver_type", 0)) == 2 else "priority"

    @property
    def waiver_budget(self) -> int:
        return int(self.settings.get("waiver_budget", 0))

    @property
    def trade_deadline_week(self) -> int | None:
        deadline = self.settings.get("trade_deadline")
        return int(deadline) if deadline is not None else None

    @property
    def playoff_week_start(self) -> int | None:
        start = self.settings.get("playoff_week_start")
        return int(start) if start is not None else None


class RosterSettings(SleeperModel):
    wins: int = 0
    losses: int = 0
    ties: int = 0
    fpts: float = 0
    fpts_decimal: float = 0
    fpts_against: float = 0
    fpts_against_decimal: float = 0
    waiver_position: int | None = None
    waiver_budget_used: int = 0

    @property
    def points_for(self) -> float:
        return round(self.fpts + self.fpts_decimal / 100, 2)

    @property
    def points_against(self) -> float:
        return round(self.fpts_against + self.fpts_against_decimal / 100, 2)

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base


class Roster(SleeperModel):
    roster_id: int
    owner_id: str | None = None
    players: list[str] | None = None
    starters: list[str] | None = None
    reserve: list[str] | None = None  # IR slots
    taxi: list[str] | None = None
    settings: RosterSettings = Field(default_factory=RosterSettings)


class LeagueUser(SleeperModel):
    user_id: str
    display_name: str | None = None
    metadata: dict[str, Any] | None = None
    is_owner: bool | None = None

    @property
    def team_name(self) -> str:
        if self.metadata and self.metadata.get("team_name"):
            return str(self.metadata["team_name"])
        return self.display_name or self.user_id


class Matchup(SleeperModel):
    roster_id: int
    matchup_id: int | None = None
    points: float = 0
    starters: list[str] | None = None
    players: list[str] | None = None
    starters_points: list[float] | None = None
    players_points: dict[str, float] | None = None


class Transaction(SleeperModel):
    transaction_id: str
    type: str  # "waiver" | "free_agent" | "trade" | "commissioner"
    status: str  # "complete" | "failed" | ...
    roster_ids: list[int] | None = None
    adds: dict[str, int] | None = None  # player_id -> roster_id
    drops: dict[str, int] | None = None
    settings: dict[str, Any] | None = None  # e.g. {"waiver_bid": 12}
    leg: int | None = None  # week
    created: int | None = None
    draft_picks: list[dict[str, Any]] = Field(default_factory=list)
    waiver_budget: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def faab_bid(self) -> int | None:
        if self.settings and "waiver_bid" in self.settings:
            return int(self.settings["waiver_bid"])
        return None


class TradedPick(SleeperModel):
    season: str
    round: int
    roster_id: int  # original owner
    previous_owner_id: int | None = None
    owner_id: int  # current owner


class Player(SleeperModel):
    player_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    position: str | None = None
    fantasy_positions: list[str] | None = None
    team: str | None = None
    active: bool | None = None
    status: str | None = None  # "Active", "Injured Reserve", ...
    injury_status: str | None = None
    depth_chart_order: int | None = None
    depth_chart_position: str | None = None
    years_exp: int | None = None

    @property
    def name(self) -> str:
        if self.full_name:
            return self.full_name
        if self.position == "DEF":
            # Team defenses are keyed by team code with city/nickname split across
            # first_name/last_name (e.g. "San Francisco" / "49ers").
            return f"{self.first_name} {self.last_name}".strip() or f"{self.team} DEF"
        if self.first_name or self.last_name:
            return f"{self.first_name or ''} {self.last_name or ''}".strip()
        return self.player_id or "?"


class TrendingPlayer(SleeperModel):
    player_id: str
    count: int


class SleeperUser(SleeperModel):
    user_id: str
    username: str | None = None
    display_name: str | None = None


# ---------------------------------------------------------------------------
# Context-side models (the WeeklyContext handed to the analyst in Phase 2)
# ---------------------------------------------------------------------------


class ContextPlayer(BaseModel):
    id: str
    name: str
    pos: str | None
    nfl_team: str | None
    injury_status: str | None = None
    depth_chart_order: int | None = None
    is_starter: bool = False
    is_ir: bool = False
    bye_week: int | None = None


class LeagueInfo(BaseModel):
    name: str
    season: str
    scoring_settings: dict[str, float]  # non-zero keys only
    roster_positions: list[str]
    waiver_type: str  # "FAAB" | "priority"
    waiver_budget: int
    trade_deadline_week: int | None
    playoff_week_start: int | None
    num_teams: int


class MyTeam(BaseModel):
    roster_id: int
    record: str
    points_for: float
    points_against: float
    faab_remaining: int | None
    waiver_position: int | None
    players: list[ContextPlayer]


class SlotPlayer(BaseModel):
    slot: str
    player: str
    player_id: str | None = None
    points: float | None = None


class BenchPlayer(BaseModel):
    player: str
    player_id: str | None = None
    points: float | None = None


class LastMatchup(BaseModel):
    opponent: str
    my_points: float
    their_points: float
    my_starters: list[SlotPlayer]
    my_bench: list[BenchPlayer]
    their_starters: list[SlotPlayer]


class UpcomingMatchup(BaseModel):
    opponent: str
    opponent_record: str
    their_starters: list[SlotPlayer]


class OtherTeam(BaseModel):
    name: str
    record: str
    faab_remaining: int | None
    positional_counts: dict[str, int]
    surplus: list[str]
    recent_adds: list[str]
    recent_drops: list[str]


class FreeAgent(BaseModel):
    id: str
    name: str
    pos: str | None
    nfl_team: str | None
    trending_add: int = 0
    trending_drop: int = 0
    injury_status: str | None = None
    depth_chart_order: int | None = None
    bye_week: int | None = None


class ActivityItem(BaseModel):
    week: int | None
    type: str
    description: str


class Standing(BaseModel):
    team: str
    wins: int
    losses: int
    ties: int
    points_for: float


class WeeklyContext(BaseModel):
    season: str
    completed_week: int
    upcoming_week: int
    league: LeagueInfo
    my_team: MyTeam
    last_matchup: LastMatchup | None
    upcoming_matchup: UpcomingMatchup | None
    other_teams: list[OtherTeam]
    free_agents: dict[str, list[FreeAgent]]  # position -> ranked free agents
    league_activity: list[ActivityItem]
    prior_analysis: dict[str, Any] | None = None  # last week's Analysis, for self-grading
    standings: list[Standing]
    directive: str | None = None  # per-run instruction (e.g. Friday injury re-check)


# ---------------------------------------------------------------------------
# Analysis (strict JSON output from Claude; every player_id must exist in the
# WeeklyContext — validated in analyze.py)
# ---------------------------------------------------------------------------


class Recap(BaseModel):
    summary: str
    what_worked: list[str] = Field(default_factory=list)
    what_hurt: list[str] = Field(default_factory=list)
    self_grade: str = ""


class LineupRec(BaseModel):
    slot: str
    player_id: str
    player: str
    reason: str


class BenchRec(BaseModel):
    player: str
    player_id: str | None = None
    reason: str


class WaiverClaim(BaseModel):
    priority: int
    add_player_id: str
    add: str
    drop_player_id: str | None = None
    drop: str | None = None
    faab_bid: int | None = None  # None in waiver-priority leagues
    reason: str


class TradeIdea(BaseModel):
    partner: str
    give: list[str]
    get: list[str]
    pitch: str
    why_it_helps_me: str
    why_they_might_accept: str


class WatchlistItem(BaseModel):
    player: str
    note: str


class Analysis(BaseModel):
    recap: Recap
    lineup: list[LineupRec]
    bench: list[BenchRec] = Field(default_factory=list)
    waivers: list[WaiverClaim] = Field(default_factory=list)
    trades: list[TradeIdea] = Field(default_factory=list)
    watchlist: list[WatchlistItem] = Field(default_factory=list)
    confidence_notes: str = ""
