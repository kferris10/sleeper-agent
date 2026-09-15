"""Shared data layer for the weekly league award show.

Fetches league-wide per-player scoring, draft rounds, and weekly projections,
then reduces them to a single `Awards` object. Two renderers consume it:
`week1_recap.py` (markdown) and `week1_deck.py` (PowerPoint), so the numbers in
the deck and the numbers in the Slack post cannot drift apart.

Deliberately standalone: it reuses SleeperClient / PlayerCache / load_settings but
touches nothing in the collect -> analyze -> deliver pipeline, so the scheduled
Tuesday/Friday run cannot be affected by it.

Two of the data sources are unofficial (draft picks, and projections, which live
on a different host entirely). Each degrades on its own: if one is unavailable the
awards that depend on it come back empty and the renderers skip those sections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from sleeper_analyst.config import Settings
from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.players import PlayerCache

logger = logging.getLogger(__name__)

PROJECTIONS_HOST = "https://api.sleeper.com"
FLEX_POSITIONS = {"RB", "WR", "TE"}
ZERO_CLUB_THRESHOLD = 2.0
LATE_ROUND_START = 9  # "drafted after round 8"
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


@dataclass
class Row:
    """One (team, player) pair for the week."""

    roster_id: int
    team: str
    player_id: str
    name: str
    pos: str
    nfl_team: str
    points: float
    projected: float | None
    started: bool
    slot: str | None
    draft_round: int | None
    draft_pick: int | None

    @property
    def vs_proj(self) -> float | None:
        return None if self.projected is None else self.points - self.projected

    @property
    def is_late_pick(self) -> bool:
        return self.draft_round is not None and self.draft_round >= LATE_ROUND_START


@dataclass
class Team:
    roster_id: int
    name: str
    manager: str
    points: float
    opponent: str
    opponent_points: float
    rows: list[Row]

    @property
    def won(self) -> bool:
        return self.points > self.opponent_points

    @property
    def margin(self) -> float:
        return self.points - self.opponent_points

    @property
    def starters(self) -> list[Row]:
        return [r for r in self.rows if r.started]

    @property
    def bench(self) -> list[Row]:
        return [r for r in self.rows if not r.started]

    @property
    def projected(self) -> float | None:
        vals = [r.projected for r in self.starters if r.projected is not None]
        return sum(vals) if vals else None


@dataclass
class Blunder:
    team: Team
    started: Row
    benched: Row
    gap: float


@dataclass
class Efficiency:
    team: Team
    optimal: float
    pct: float


@dataclass
class Awards:
    """Everything a renderer needs, already sorted and trimmed."""

    week: int
    teams: list[Team]
    have_projections: bool
    have_draft: bool

    league_average: float = 0.0
    champ: Team | None = None
    champ_top: list[Row] = field(default_factory=list)
    chump: Team | None = None
    chump_duds: list[Row] = field(default_factory=list)
    chump_heroes: list[Row] = field(default_factory=list)
    overachievers: list[Row] = field(default_factory=list)
    benched_overachiever: Row | None = None
    busts: list[Row] = field(default_factory=list)
    late_steals: list[Row] = field(default_factory=list)
    late_steals_started: list[Row] = field(default_factory=list)
    blunders: list[Blunder] = field(default_factory=list)
    efficiency: list[Efficiency] = field(default_factory=list)
    unlucky: Team | None = None
    lucky: Team | None = None
    closest: Team | None = None
    blowout: Team | None = None
    zero_club: list[Row] = field(default_factory=list)
    undrafted: list[Row] = field(default_factory=list)
    vs_projection: list[Team] = field(default_factory=list)
    beat_projection: int = 0
    positional_best: list[tuple[str, Row]] = field(default_factory=list)


# -- fetching ---------------------------------------------------------------


def fetch_draft_picks(
    client: SleeperClient, league_id: str
) -> dict[str, tuple[int, int]]:
    """player_id -> (round, overall pick). Empty dict if unavailable."""
    try:
        drafts = client._get(f"/league/{league_id}/drafts")
        if not drafts:
            return {}
        picks = client._get(f"/draft/{drafts[0]['draft_id']}/picks")
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        logger.warning("Draft picks unavailable (%s); skipping draft-round awards", exc)
        return {}
    return {
        p["player_id"]: (p["round"], p["pick_no"]) for p in picks if p.get("player_id")
    }


def fetch_projections(season: str, week: int) -> dict[str, float]:
    """player_id -> projected PPR points. Empty dict if unavailable.

    The league is full PPR on otherwise-default scoring (rec 1.0, pass_td 4,
    pass_yd 0.04, rush/rec_yd 0.1), so Sleeper's precomputed pts_ppr lines up
    with how this league actually scores.
    """
    params = {
        "season_type": "regular",
        "position[]": POSITION_ORDER,
        "order_by": "pts_ppr",
    }
    try:
        resp = httpx.get(
            f"{PROJECTIONS_HOST}/projections/nfl/{season}/{week}",
            params=params,
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Projections unavailable (%s); skipping projection awards", exc)
        return {}

    out: dict[str, float] = {}
    for entry in payload:
        pid = entry.get("player_id")
        pts = (entry.get("stats") or {}).get("pts_ppr")
        if pid and pts is not None:
            out[str(pid)] = float(pts)
    return out


def slot_labels(roster_positions: list[str]) -> list[str]:
    """['QB','RB','RB',...] -> ['QB','RB1','RB2',...], bench slots dropped."""
    counts: dict[str, int] = {}
    totals: dict[str, int] = {}
    for pos in roster_positions:
        if pos != "BN":
            totals[pos] = totals.get(pos, 0) + 1
    labels = []
    for pos in roster_positions:
        if pos == "BN":
            continue
        counts[pos] = counts.get(pos, 0) + 1
        labels.append(f"{pos}{counts[pos]}" if totals[pos] > 1 else pos)
    return labels


def build_teams(
    settings: Settings, client: SleeperClient, week: int
) -> tuple[list[Team], list[str], bool, bool]:
    league_id = settings.league.league_id
    league = client.get_league(league_id)
    matchups = client.get_matchups(league_id, week)
    rosters = client.get_rosters(league_id)
    users = {u.user_id: u for u in client.get_users(league_id)}
    players = PlayerCache(settings.data_dir).get(client)

    draft = fetch_draft_picks(client, league_id)
    projections = fetch_projections(league.season, week)

    roster_positions = league.roster_positions or []
    owner_of = {r.roster_id: r.owner_id for r in rosters}
    labels = slot_labels(roster_positions)

    def team_name(roster_id: int) -> str:
        user = users.get(owner_of.get(roster_id) or "")
        return user.team_name if user else f"Roster {roster_id}"

    def manager(roster_id: int) -> str:
        user = users.get(owner_of.get(roster_id) or "")
        return (user.display_name or "?") if user else "?"

    pairs: dict[int, list[int]] = {}
    for m in matchups:
        if m.matchup_id is not None:
            pairs.setdefault(m.matchup_id, []).append(m.roster_id)
    points_by_roster = {m.roster_id: m.points for m in matchups}

    teams: list[Team] = []
    for m in matchups:
        starters = m.starters or []
        starter_pts = m.starters_points or []
        all_pts = m.players_points or {}

        slot_of: dict[str, str] = {}
        # starters_points is index-aligned to starters and is the authoritative
        # per-slot score, so carry it alongside the slot label.
        slot_points: dict[str, float] = {}
        for idx, pid in enumerate(starters):
            if not pid or pid == "0":
                continue
            slot_of[pid] = labels[idx] if idx < len(labels) else f"S{idx + 1}"
            if idx < len(starter_pts):
                slot_points[pid] = float(starter_pts[idx])

        rows = []
        for pid in m.players or []:
            player = players.get(pid)
            pick = draft.get(pid)
            rows.append(
                Row(
                    roster_id=m.roster_id,
                    team=team_name(m.roster_id),
                    player_id=pid,
                    name=(player.name if player else pid),
                    pos=(player.position if player else None) or "?",
                    nfl_team=(player.team if player else None) or "FA",
                    points=slot_points.get(pid, float(all_pts.get(pid) or 0.0)),
                    projected=projections.get(pid),
                    started=pid in slot_of,
                    slot=slot_of.get(pid),
                    draft_round=pick[0] if pick else None,
                    draft_pick=pick[1] if pick else None,
                )
            )

        opponent_ids = [r for r in pairs.get(m.matchup_id or -1, []) if r != m.roster_id]
        opp_id = opponent_ids[0] if opponent_ids else None
        teams.append(
            Team(
                roster_id=m.roster_id,
                name=team_name(m.roster_id),
                manager=manager(m.roster_id),
                points=m.points,
                opponent=team_name(opp_id) if opp_id else "bye",
                opponent_points=points_by_roster.get(opp_id, 0.0) if opp_id else 0.0,
                rows=rows,
            )
        )

    return teams, roster_positions, bool(projections), bool(draft)


# -- award math -------------------------------------------------------------


def optimal_lineup(rows: list[Row], roster_positions: list[str]) -> float:
    """Best legal lineup score, greedy over QB/RB/RB/WR/WR/TE/FLEX/K/DEF.

    Greedy is optimal here: every fixed slot takes a single position, so filling
    them best-first leaves the FLEX the best remaining RB/WR/TE either way.
    """
    pool = sorted(rows, key=lambda r: r.points, reverse=True)
    used: set[str] = set()
    total = 0.0
    for slot in [p for p in roster_positions if p not in ("BN", "FLEX")]:
        for row in pool:
            if row.player_id not in used and row.pos == slot:
                used.add(row.player_id)
                total += row.points
                break
    for _ in [p for p in roster_positions if p == "FLEX"]:
        for row in pool:
            if row.player_id not in used and row.pos in FLEX_POSITIONS:
                used.add(row.player_id)
                total += row.points
                break
    return total


def worst_start_sit(team: Team) -> Blunder | None:
    """The most embarrassing start/sit pair at a slot-compatible position."""
    best: Blunder | None = None
    for sat in team.bench:
        for started in team.starters:
            slot = started.slot or ""
            compatible = started.pos == sat.pos or (
                slot.startswith("FLEX") and sat.pos in FLEX_POSITIONS
            )
            if not compatible:
                continue
            gap = sat.points - started.points
            if gap > 0 and (best is None or gap > best.gap):
                best = Blunder(team=team, started=started, benched=sat, gap=gap)
    return best


def compute_awards(
    teams: list[Team],
    roster_positions: list[str],
    week: int,
    have_projections: bool,
    have_draft: bool,
) -> Awards:
    """Reduce the week to a sorted, trimmed set of award winners.

    Rosters are exclusive in a redraft league, so every player appears on at most
    one team and each Row already names its owner. No cross-team dedupe needed.
    """
    a = Awards(
        week=week,
        teams=teams,
        have_projections=have_projections,
        have_draft=have_draft,
    )
    if not teams:
        return a

    all_rows = [r for t in teams for r in t.rows]
    started = [r for r in all_rows if r.started]
    by_score = sorted(teams, key=lambda t: t.points, reverse=True)

    a.league_average = sum(t.points for t in teams) / len(teams)
    a.champ, a.chump = by_score[0], by_score[-1]
    a.champ_top = sorted(a.champ.starters, key=lambda r: r.points, reverse=True)[:3]
    a.chump_duds = sorted(a.chump.starters, key=lambda r: r.points)[:3]
    a.chump_heroes = sorted(a.chump.starters, key=lambda r: r.points, reverse=True)[:2]

    if have_projections:
        scored = [r for r in started if r.vs_proj is not None]
        a.overachievers = sorted(scored, key=lambda r: r.vs_proj or 0, reverse=True)[:3]
        a.busts = sorted(scored, key=lambda r: r.vs_proj or 0)[:3]
        benched = [r for r in all_rows if not r.started and r.vs_proj is not None]
        a.benched_overachiever = (
            max(benched, key=lambda r: r.vs_proj or 0) if benched else None
        )
        with_proj = [t for t in teams if t.projected is not None]
        a.vs_projection = sorted(
            with_proj, key=lambda t: t.points - (t.projected or 0), reverse=True
        )
        a.beat_projection = sum(1 for t in with_proj if t.points > (t.projected or 0))

    if have_draft:
        a.late_steals = sorted(
            [r for r in all_rows if r.is_late_pick],
            key=lambda r: r.points,
            reverse=True,
        )[:3]
        a.late_steals_started = sorted(
            [r for r in started if r.is_late_pick],
            key=lambda r: r.points,
            reverse=True,
        )[:3]
        a.undrafted = sorted(
            [r for r in started if r.draft_round is None],
            key=lambda r: r.points,
            reverse=True,
        )[:3]

    blunders = [b for b in (worst_start_sit(t) for t in teams) if b]
    a.blunders = sorted(blunders, key=lambda b: b.gap, reverse=True)[:3]

    eff = []
    for t in teams:
        best = optimal_lineup(t.rows, roster_positions)
        eff.append(
            Efficiency(team=t, optimal=best, pct=(t.points / best * 100) if best else 0.0)
        )
    a.efficiency = sorted(eff, key=lambda e: e.pct, reverse=True)

    losers = [t for t in teams if not t.won and t.margin != 0]
    winners = [t for t in teams if t.won]
    a.unlucky = max(losers, key=lambda t: t.points) if losers else None
    a.lucky = min(winners, key=lambda t: t.points) if winners else None

    # one entry per game, keyed so the two rosters collapse to a single row
    games: dict[tuple[str, ...], Team] = {}
    for t in teams:
        games.setdefault(tuple(sorted([t.name, t.opponent])), t)
    by_margin = sorted(games.values(), key=lambda t: abs(t.margin))
    a.closest, a.blowout = by_margin[0], by_margin[-1]

    a.zero_club = sorted(
        [r for r in started if r.points <= ZERO_CLUB_THRESHOLD],
        key=lambda r: (r.points, r.team),
    )

    for pos in POSITION_ORDER:
        at_pos = [r for r in started if r.pos == pos]
        if at_pos:
            a.positional_best.append((pos, max(at_pos, key=lambda r: r.points)))

    return a


def load_awards(settings: Settings, week: int) -> Awards:
    with SleeperClient() as client:
        teams, roster_positions, have_proj, have_draft = build_teams(
            settings, client, week
        )
    return compute_awards(teams, roster_positions, week, have_proj, have_draft)


# -- formatting -------------------------------------------------------------


def fmt(points: float) -> str:
    return f"{points:.1f}"


def signed(points: float) -> str:
    return f"{points:+.1f}"


def drafted_note(row: Row) -> str:
    if row.draft_round is None:
        return "undrafted"
    return f"round {row.draft_round}, pick {row.draft_pick} overall"
