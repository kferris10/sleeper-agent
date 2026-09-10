"""Build the WeeklyContext: everything the analyst needs to know about the league."""

from __future__ import annotations

import logging

from sleeper_analyst.config import Settings
from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.models import (
    ActivityItem,
    BenchPlayer,
    ContextPlayer,
    FreeAgent,
    LastMatchup,
    League,
    LeagueInfo,
    LeagueUser,
    Matchup,
    MyTeam,
    OtherTeam,
    Player,
    Roster,
    SlotPlayer,
    Standing,
    Transaction,
    UpcomingMatchup,
    WeeklyContext,
)

logger = logging.getLogger(__name__)

# Positions a flex-type roster slot can be filled by
FLEX_ELIGIBLE: dict[str, list[str]] = {
    "FLEX": ["RB", "WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
    "REC_FLEX": ["WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
    "IDP_FLEX": ["DL", "LB", "DB"],
}

# How many free agents to keep per position (~40 total for a standard league)
FA_LIMITS: dict[str, int] = {"QB": 6, "RB": 10, "WR": 10, "TE": 6, "K": 4, "DEF": 4}
FA_DEFAULT_LIMIT = 5


def slot_names(roster_positions: list[str]) -> list[str]:
    """Starting slot labels aligned with a roster's `starters` array.

    Sleeper's starters list follows roster_positions order with BN/IR/TAXI
    excluded. Repeated positions get numbered: [QB, RB, RB, FLEX] -> [QB, RB1, RB2, FLEX].
    """
    starting = [p for p in roster_positions if p not in ("BN", "IR", "TAXI")]
    counts = {p: starting.count(p) for p in starting}
    seen: dict[str, int] = {}
    names = []
    for pos in starting:
        if counts[pos] > 1:
            seen[pos] = seen.get(pos, 0) + 1
            names.append(f"{pos}{seen[pos]}")
        else:
            names.append(pos)
    return names


def fantasy_positions(roster_positions: list[str]) -> set[str]:
    """Concrete player positions usable in this league's lineup."""
    positions: set[str] = set()
    for slot in roster_positions:
        if slot in ("BN", "IR", "TAXI"):
            continue
        positions.update(FLEX_ELIGIBLE.get(slot, [slot]))
    return positions


def pair_matchups(matchups: list[Matchup]) -> dict[int, int]:
    """roster_id -> opponent roster_id, paired via matchup_id."""
    by_matchup: dict[int, list[int]] = {}
    for m in matchups:
        if m.matchup_id is not None:
            by_matchup.setdefault(m.matchup_id, []).append(m.roster_id)
    pairs: dict[int, int] = {}
    for roster_ids in by_matchup.values():
        if len(roster_ids) == 2:
            a, b = roster_ids
            pairs[a], pairs[b] = b, a
    return pairs


def _player_name(player_id: str | None, players: dict[str, Player]) -> str:
    if player_id is None or player_id in ("", "0"):
        return "(empty)"
    player = players.get(player_id)
    return player.name if player else player_id


def _context_player(
    player_id: str, players: dict[str, Player], roster: Roster, byes: dict[str, int]
) -> ContextPlayer:
    p = players.get(player_id)
    team = p.team if p else None
    return ContextPlayer(
        id=player_id,
        name=_player_name(player_id, players),
        pos=p.position if p else None,
        nfl_team=team,
        injury_status=p.injury_status if p else None,
        depth_chart_order=p.depth_chart_order if p else None,
        is_starter=player_id in (roster.starters or []),
        is_ir=player_id in (roster.reserve or []),
        bye_week=byes.get(team.upper()) if team else None,
    )


def _starters_with_slots(
    matchup: Matchup, roster_positions: list[str], players: dict[str, Player]
) -> list[SlotPlayer]:
    slots = slot_names(roster_positions)
    starters = matchup.starters or []
    points = matchup.starters_points or []
    result = []
    for i, player_id in enumerate(starters):
        result.append(
            SlotPlayer(
                slot=slots[i] if i < len(slots) else f"SLOT{i + 1}",
                player=_player_name(player_id, players),
                player_id=player_id if player_id not in ("", "0") else None,
                points=points[i] if i < len(points) else None,
            )
        )
    return result


def _roster_starters_with_slots(
    roster: Roster, roster_positions: list[str], players: dict[str, Player]
) -> list[SlotPlayer]:
    slots = slot_names(roster_positions)
    result = []
    for i, player_id in enumerate(roster.starters or []):
        result.append(
            SlotPlayer(
                slot=slots[i] if i < len(slots) else f"SLOT{i + 1}",
                player=_player_name(player_id, players),
                player_id=player_id if player_id not in ("", "0") else None,
            )
        )
    return result


def humanize_transaction(
    tx: Transaction,
    roster_names: dict[int, str],
    players: dict[str, Player],
) -> str:
    def name(rid: int) -> str:
        return roster_names.get(rid, f"Roster {rid}")

    adds = [
        f"{_player_name(pid, players)} ({name(rid)})" for pid, rid in (tx.adds or {}).items()
    ]
    drops = [
        f"{_player_name(pid, players)} ({name(rid)})" for pid, rid in (tx.drops or {}).items()
    ]
    parts = []
    if adds:
        parts.append("added " + ", ".join(adds))
    if drops:
        parts.append("dropped " + ", ".join(drops))
    desc = "; ".join(parts) if parts else "no players moved"
    label = {"waiver": "Waiver", "free_agent": "Free agent", "trade": "Trade", "commissioner": "Commish"}.get(
        tx.type, tx.type
    )
    bid = f" (${tx.faab_bid} FAAB)" if tx.faab_bid is not None else ""
    failed = " [FAILED]" if tx.status != "complete" else ""
    return f"{label}: {desc}{bid}{failed}"


def compute_free_agents(
    players: dict[str, Player],
    rosters: list[Roster],
    league_positions: set[str],
    trending_add: dict[str, int],
    trending_drop: dict[str, int],
    byes: dict[str, int],
) -> dict[str, list[FreeAgent]]:
    """active players at league positions − union of all rostered players, ranked by trending adds."""
    rostered: set[str] = set()
    for roster in rosters:
        rostered.update(roster.players or [])

    candidates: dict[str, list[FreeAgent]] = {}
    for player_id, p in players.items():
        if player_id in rostered or not p.active or p.position not in league_positions:
            continue
        candidates.setdefault(p.position, []).append(
            FreeAgent(
                id=player_id,
                name=p.name,
                pos=p.position,
                nfl_team=p.team,
                trending_add=trending_add.get(player_id, 0),
                trending_drop=trending_drop.get(player_id, 0),
                injury_status=p.injury_status,
                depth_chart_order=p.depth_chart_order,
                bye_week=byes.get(p.team.upper()) if p.team else None,
            )
        )

    result: dict[str, list[FreeAgent]] = {}
    for pos, agents in sorted(candidates.items()):
        # Trending adds first, then depth chart position; unrostered deep-bench
        # players with neither signal sort last.
        agents.sort(key=lambda a: (-a.trending_add, a.depth_chart_order or 99, a.name))
        result[pos] = agents[: FA_LIMITS.get(pos, FA_DEFAULT_LIMIT)]
    return result


def _positional_counts(roster: Roster, players: dict[str, Player]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pid in roster.players or []:
        p = players.get(pid)
        if p and p.position:
            counts[p.position] = counts.get(p.position, 0) + 1
    return dict(sorted(counts.items()))


def _surplus(counts: dict[str, int], roster_positions: list[str]) -> list[str]:
    """Positions where a team carries clearly more than it can start (direct slots + 2)."""
    direct_slots: dict[str, int] = {}
    for slot in roster_positions:
        if slot not in ("BN", "IR", "TAXI") and slot not in FLEX_ELIGIBLE:
            direct_slots[slot] = direct_slots.get(slot, 0) + 1
    return [
        pos
        for pos, n in counts.items()
        if pos in direct_slots and n > direct_slots[pos] + 2
    ]


def build_weekly_context(
    client: SleeperClient,
    players: dict[str, Player],
    settings: Settings,
    week: int | None = None,
) -> WeeklyContext:
    state = client.get_state()
    league: League = client.get_league(settings.league.league_id)
    rosters = client.get_rosters(settings.league.league_id)
    users = client.get_users(settings.league.league_id)

    upcoming_week = week if week is not None else state.week
    completed_week = upcoming_week - 1

    users_by_id: dict[str, LeagueUser] = {u.user_id: u for u in users}
    roster_names: dict[int, str] = {}
    for roster in rosters:
        owner = users_by_id.get(roster.owner_id or "")
        roster_names[roster.roster_id] = owner.team_name if owner else f"Roster {roster.roster_id}"

    my_roster = next(
        (r for r in rosters if r.owner_id == settings.league.user_id), None
    )
    if my_roster is None:
        owners = ", ".join(
            f"{users_by_id.get(r.owner_id or '', LeagueUser(user_id='?')).display_name} ({r.owner_id})"
            for r in rosters
        )
        raise ValueError(
            f"No roster owned by user_id={settings.league.user_id!r} in league "
            f"{settings.league.league_id}. Owners: {owners}"
        )

    byes = {team.upper(): wk for team, wk in settings.byes.items()}
    is_faab = league.waiver_type == "FAAB"

    def faab_remaining(roster: Roster) -> int | None:
        if not is_faab:
            return None
        return league.waiver_budget - roster.settings.waiver_budget_used

    # -- my team -----------------------------------------------------------
    my_team = MyTeam(
        roster_id=my_roster.roster_id,
        record=my_roster.settings.record,
        points_for=my_roster.settings.points_for,
        points_against=my_roster.settings.points_against,
        faab_remaining=faab_remaining(my_roster),
        waiver_position=my_roster.settings.waiver_position,
        players=[
            _context_player(pid, players, my_roster, byes)
            for pid in (my_roster.players or [])
        ],
    )

    # -- last week's matchup -------------------------------------------------
    last_matchup: LastMatchup | None = None
    if completed_week >= 1:
        last_matchups = client.get_matchups(settings.league.league_id, completed_week)
        by_roster = {m.roster_id: m for m in last_matchups}
        pairs = pair_matchups(last_matchups)
        mine = by_roster.get(my_roster.roster_id)
        opp_id = pairs.get(my_roster.roster_id)
        if mine is not None and opp_id is not None:
            theirs = by_roster[opp_id]
            starter_ids = set(mine.starters or [])
            bench = [
                BenchPlayer(
                    player=_player_name(pid, players),
                    player_id=pid,
                    points=(mine.players_points or {}).get(pid),
                )
                for pid in (mine.players or [])
                if pid not in starter_ids
            ]
            last_matchup = LastMatchup(
                opponent=roster_names.get(opp_id, f"Roster {opp_id}"),
                my_points=mine.points,
                their_points=theirs.points,
                my_starters=_starters_with_slots(mine, league.roster_positions, players),
                my_bench=bench,
                their_starters=_starters_with_slots(theirs, league.roster_positions, players),
            )

    # -- upcoming matchup ----------------------------------------------------
    upcoming_matchup: UpcomingMatchup | None = None
    upcoming = client.get_matchups(settings.league.league_id, upcoming_week)
    pairs = pair_matchups(upcoming)
    opp_id = pairs.get(my_roster.roster_id)
    if opp_id is not None:
        opp_roster = next((r for r in rosters if r.roster_id == opp_id), None)
        if opp_roster is not None:
            upcoming_matchup = UpcomingMatchup(
                opponent=roster_names.get(opp_id, f"Roster {opp_id}"),
                opponent_record=opp_roster.settings.record,
                # Pre-game matchup starters mirror current roster starters; use the roster's.
                their_starters=_roster_starters_with_slots(
                    opp_roster, league.roster_positions, players
                ),
            )

    # -- transactions (last 2 weeks) -----------------------------------------
    tx_weeks = sorted({w for w in (completed_week - 1, completed_week, upcoming_week) if w >= 1})
    transactions: list[Transaction] = []
    for w in tx_weeks:
        transactions.extend(client.get_transactions(settings.league.league_id, w))
    transactions.sort(key=lambda t: t.created or 0, reverse=True)

    league_activity = [
        ActivityItem(week=tx.leg, type=tx.type, description=humanize_transaction(tx, roster_names, players))
        for tx in transactions
    ]

    # recent adds/drops per roster (completed transactions only)
    recent_adds: dict[int, list[str]] = {}
    recent_drops: dict[int, list[str]] = {}
    for tx in transactions:
        if tx.status != "complete":
            continue
        for pid, rid in (tx.adds or {}).items():
            recent_adds.setdefault(rid, []).append(_player_name(pid, players))
        for pid, rid in (tx.drops or {}).items():
            recent_drops.setdefault(rid, []).append(_player_name(pid, players))

    # -- other teams -----------------------------------------------------------
    other_teams = []
    for roster in rosters:
        if roster.roster_id == my_roster.roster_id:
            continue
        counts = _positional_counts(roster, players)
        other_teams.append(
            OtherTeam(
                name=roster_names[roster.roster_id],
                record=roster.settings.record,
                faab_remaining=faab_remaining(roster),
                positional_counts=counts,
                surplus=_surplus(counts, league.roster_positions),
                recent_adds=recent_adds.get(roster.roster_id, []),
                recent_drops=recent_drops.get(roster.roster_id, []),
            )
        )

    # -- free agents -----------------------------------------------------------
    trending_add = {t.player_id: t.count for t in client.get_trending("add")}
    trending_drop = {t.player_id: t.count for t in client.get_trending("drop")}
    free_agents = compute_free_agents(
        players,
        rosters,
        fantasy_positions(league.roster_positions),
        trending_add,
        trending_drop,
        byes,
    )

    # -- standings --------------------------------------------------------------
    standings = sorted(
        (
            Standing(
                team=roster_names[r.roster_id],
                wins=r.settings.wins,
                losses=r.settings.losses,
                ties=r.settings.ties,
                points_for=r.settings.points_for,
            )
            for r in rosters
        ),
        key=lambda s: (-s.wins, -s.points_for),
    )

    return WeeklyContext(
        season=league.season,
        completed_week=completed_week,
        upcoming_week=upcoming_week,
        league=LeagueInfo(
            name=league.name,
            season=league.season,
            scoring_settings={k: v for k, v in league.scoring_settings.items() if v},
            roster_positions=league.roster_positions,
            waiver_type=league.waiver_type,
            waiver_budget=league.waiver_budget,
            trade_deadline_week=league.trade_deadline_week,
            playoff_week_start=league.playoff_week_start,
            num_teams=league.total_rosters,
        ),
        my_team=my_team,
        last_matchup=last_matchup,
        upcoming_matchup=upcoming_matchup,
        other_teams=other_teams,
        free_agents=free_agents,
        league_activity=league_activity,
        prior_analysis=None,
        standings=standings,
    )
