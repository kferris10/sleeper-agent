"""Shared data layer for the weekly league award show.

Fetches league-wide per-player scoring, draft rounds, and weekly projections,
then reduces them to a single `Awards` object. Two renderers consume it:
`league_recap.py` (markdown) and `league_deck.py` (PowerPoint), so the numbers in
the deck, the markdown recap and the weekly email cannot drift apart.

Deliberately standalone: it reuses SleeperClient / PlayerCache / load_settings but
touches nothing in the collect -> analyze -> deliver pipeline, so the scheduled
Tuesday/Friday run cannot be affected by it.

Three of the data sources are unofficial (draft picks, and projections and box-score
stats, which live on a different host entirely). Each degrades on its own: if one is
unavailable the awards that depend on it come back empty and the renderers skip those
sections.
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
TOUCH_POSITIONS = {"RB", "WR", "TE"}  # positions whose job is carries and catches
SHAME_LIMIT = 9  # entries the recap and the email carry
SHAME_SLIDE_ROWS = 6  # entries that fit on one slide without wrapping


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
    stats: dict[str, float] | None = None

    @property
    def vs_proj(self) -> float | None:
        return None if self.projected is None else self.points - self.projected

    @property
    def is_late_pick(self) -> bool:
        return self.draft_round is not None and self.draft_round >= LATE_ROUND_START

    @property
    def touches(self) -> float | None:
        """Carries + catches, or None when the box score is unavailable."""
        if self.stats is None:
            return None
        return self.stats.get("rush_att", 0.0) + self.stats.get("rec", 0.0)

    @property
    def played(self) -> bool | None:
        """Did he suit up at all? None when the box score is unavailable."""
        if self.stats is None:
            return None
        return bool(self.stats.get("gp", 0.0))


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
    def bench_points(self) -> float:
        return sum(r.points for r in self.bench)

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
class Shame:
    """One entry on the wall of shame: what happened, to whom, how bad."""

    award: str
    team: str
    detail: str
    severity: float


@dataclass
class Awards:
    """Everything a renderer needs, already sorted and trimmed."""

    week: int
    teams: list[Team]
    have_projections: bool
    have_draft: bool
    have_stats: bool = False

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
    shame: list[Shame] = field(default_factory=list)


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


def _fetch_weekly(kind: str, season: str, week: int) -> list[dict]:
    """One week of league-wide player lines from the unofficial host.

    `kind` is "projections" or "stats" -- same shape, same params, different
    path. Returns [] on any failure so the caller can degrade.
    """
    params = {
        "season_type": "regular",
        "position[]": POSITION_ORDER,
        "order_by": "pts_ppr",
    }
    try:
        resp = httpx.get(
            f"{PROJECTIONS_HOST}/{kind}/nfl/{season}/{week}",
            params=params,
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("%s unavailable (%s); skipping the awards that need it", kind, exc)
        return []
    return payload if isinstance(payload, list) else []


def fetch_projections(season: str, week: int) -> dict[str, float]:
    """player_id -> projected PPR points. Empty dict if unavailable.

    The league is full PPR on otherwise-default scoring (rec 1.0, pass_td 4,
    pass_yd 0.04, rush/rec_yd 0.1), so Sleeper's precomputed pts_ppr lines up
    with how this league actually scores.
    """
    out: dict[str, float] = {}
    for entry in _fetch_weekly("projections", season, week):
        pid = entry.get("player_id")
        pts = (entry.get("stats") or {}).get("pts_ppr")
        if pid and pts is not None:
            out[str(pid)] = float(pts)
    return out


def fetch_stats(season: str, week: int) -> dict[str, dict[str, float]]:
    """player_id -> that week's box score. Empty dict if unavailable.

    Only the wall of shame needs this: the matchup endpoint gives points but no
    usage, so "started a back who never got a carry" is unanswerable without it.
    A player with no line at all is left out rather than assumed benched -- an
    absent entry means the feed did not know about him, not that he did nothing.
    """
    out: dict[str, dict[str, float]] = {}
    for entry in _fetch_weekly("stats", season, week):
        pid = entry.get("player_id")
        stats = entry.get("stats")
        if pid and isinstance(stats, dict):
            out[str(pid)] = {
                k: float(v) for k, v in stats.items() if isinstance(v, (int, float))
            }
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
) -> tuple[list[Team], list[str], bool, bool, bool]:
    league_id = settings.league.league_id
    league = client.get_league(league_id)
    matchups = client.get_matchups(league_id, week)
    rosters = client.get_rosters(league_id)
    users = {u.user_id: u for u in client.get_users(league_id)}
    players = PlayerCache(settings.data_dir).get(client)

    draft = fetch_draft_picks(client, league_id)
    projections = fetch_projections(league.season, week)
    stats = fetch_stats(league.season, week)

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
                    stats=stats.get(pid),
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

    return teams, roster_positions, bool(projections), bool(draft), bool(stats)


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


def compute_shame(
    teams: list[Team],
    blunders: list[Blunder],
    efficiency: list[Efficiency],
    have_stats: bool,
) -> list[Shame]:
    """The week's self-inflicted wounds, worst category first.

    Every check is independent and skips itself when its inputs are missing, so
    a quiet week simply produces a shorter wall. `blunders` and `efficiency` are
    the untrimmed lists -- the wall wants every team's worst click, not the top
    three the Bench Burner slide shows.
    """
    out: list[Shame] = []
    if not teams:
        return out

    counts: dict[str, int] = {}
    started = [r for t in teams for r in t.rows if r.started]
    best_starter = max(started, key=lambda r: r.points) if started else None
    blunder_of = {b.team.roster_id: b for b in blunders}

    def add(award: str, team: str, detail: str, severity: float, cap: int = 2) -> None:
        """Record one entry, keeping the worst `cap` of each category.

        Without the cap a wide category (half the league gets outscored by one
        player most weeks) would fill all nine rows and crowd out the rarer,
        funnier ones. Callers feed each category worst-first.
        """
        if counts.get(award, 0) >= cap:
            return
        counts[award] = counts.get(award, 0) + 1
        out.append(Shame(award=award, team=team, detail=detail, severity=severity))

    # The bench beat the starters.
    for t in sorted(teams, key=lambda t: t.bench_points - t.points, reverse=True):
        gap = t.bench_points - t.points
        if gap > 0:
            add(
                "Wrong Nine",
                t.name,
                f"The bench outscored the starting lineup {fmt(t.bench_points)} "
                f"to {fmt(t.points)}.",
                gap,
            )

    # Best player on the roster never left the bench.
    for t in teams:
        top = max(t.rows, key=lambda r: r.points, default=None)
        if top is None or top.started or top.points <= 0:
            continue
        best_started = max((r.points for r in t.starters), default=0.0)
        add(
            "Best Seat in the House",
            t.name,
            f"{top.name} ({top.pos}) led the whole roster with {fmt(top.points)} "
            f"and watched from the bench; no starter cleared {fmt(best_started)}.",
            top.points - best_started,
        )

    # Had the week's best player and lost anyway.
    if best_starter is not None:
        owner = next(
            (t for t in teams if t.roster_id == best_starter.roster_id), None
        )
        if owner is not None and not owner.won and owner.margin != 0:
            add(
                "Wasted the Best Player Alive",
                owner.name,
                f"{best_starter.name} was the highest-scoring starter in the league "
                f"({fmt(best_starter.points)}) and {owner.name} still lost to "
                f"{owner.opponent} by {fmt(abs(owner.margin))}.",
                abs(owner.margin),
            )

    # Started a skill player who never touched the ball.
    if have_stats:
        ghosts = [
            r
            for r in started
            if r.pos in TOUCH_POSITIONS and r.touches == 0 and r.played is not None
        ]
        for r in sorted(ghosts, key=lambda r: (r.team, r.name)):
            if r.played:
                detail = (
                    f"{r.name} ({r.pos}) dressed, played, and finished with zero "
                    f"carries and zero catches."
                )
            else:
                detail = (
                    f"{r.name} ({r.pos}) never took the field at all — "
                    f"started anyway."
                )
            add(
                "Ghost in the Lineup",
                r.team,
                detail,
                25.0 if not r.played else 20.0,
                cap=3,
            )

    # A single bench decision cost the game.
    for t in teams:
        b = blunder_of.get(t.roster_id)
        if b and not t.won and t.margin != 0 and b.gap > abs(t.margin):
            add(
                "Lost It on the Bench",
                t.name,
                f"Sat {b.benched.name} ({fmt(b.benched.points)}) for "
                f"{b.started.name} ({fmt(b.started.points)}) and lost by "
                f"{fmt(abs(t.margin))}. That click was the game.",
                b.gap - abs(t.margin),
            )

    # Outscored by one man.
    if best_starter is not None:
        for t in sorted(teams, key=lambda t: t.points):
            if t.roster_id != best_starter.roster_id and t.points < best_starter.points:
                add(
                    "Outscored by One Guy",
                    t.name,
                    f"Nine starters managed {fmt(t.points)}. {best_starter.name} "
                    f"managed {fmt(best_starter.points)} by himself.",
                    best_starter.points - t.points,
                )

    # Lost to the weakest winning score of the week.
    winners = [t for t in teams if t.won]
    if winners:
        weakest = min(winners, key=lambda t: t.points)
        victim = next((t for t in teams if t.name == weakest.opponent), None)
        if victim is not None:
            add(
                "Lost to the Weakest Winner",
                victim.name,
                f"{weakest.name} posted the lowest winning score of the week "
                f"({fmt(weakest.points)}) and {victim.name} still found a way to "
                f"score less.",
                weakest.points - victim.points,
            )

    # Left the most points on the table.
    if efficiency:
        worst = min(efficiency, key=lambda e: e.pct)
        if worst.pct < 100:
            add(
                "Points Left on the Table",
                worst.team.name,
                f"Scored {fmt(worst.team.points)} of an available "
                f"{fmt(worst.optimal)} — {worst.pct:.0f}% of the roster's best week.",
                worst.optimal - worst.team.points,
            )

    return out[:SHAME_LIMIT]


def compute_awards(
    teams: list[Team],
    roster_positions: list[str],
    week: int,
    have_projections: bool,
    have_draft: bool,
    have_stats: bool = False,
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
        have_stats=have_stats,
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

    a.shame = compute_shame(teams, blunders, eff, have_stats)

    return a


def load_awards(settings: Settings, week: int | None = None) -> Awards:
    """Awards for `week`, or for the most recently completed week if omitted.

    Mirrors collect.build_weekly_context: /state/nfl reports the upcoming week,
    so the week with results in it is the one before.
    """
    with SleeperClient() as client:
        if week is None:
            week = client.get_state().week - 1
            if week < 1:
                raise ValueError(
                    "No completed week yet this season — pass --week explicitly."
                )
        teams, roster_positions, have_proj, have_draft, have_stats = build_teams(
            settings, client, week
        )
    return compute_awards(
        teams, roster_positions, week, have_proj, have_draft, have_stats
    )


# -- formatting -------------------------------------------------------------


def fmt(points: float) -> str:
    return f"{points:.1f}"


def signed(points: float) -> str:
    return f"{points:+.1f}"


def drafted_note(row: Row) -> str:
    if row.draft_round is None:
        return "undrafted"
    return f"round {row.draft_round}, pick {row.draft_pick} overall"
