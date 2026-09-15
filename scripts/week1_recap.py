"""One-off: a playful, shareable Week 1 league recap ("The Chace's 41st Awards").

Pulls league-wide per-player scoring, draft rounds, and weekly projections, then
prints a markdown award show to stdout and writes data/recap_week_N.md.

Deliberately standalone: it reuses SleeperClient / PlayerCache / load_settings but
touches nothing in the collect -> analyze -> deliver pipeline, so the scheduled
Tuesday/Friday run cannot be affected by it.

Two of the data sources are unofficial (draft picks, and projections, which live
on a different host entirely). Each degrades on its own: if one is unavailable the
awards that depend on it are skipped with a note instead of taking the recap down.

    uv run python scripts/week1_recap.py --week 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from sleeper_analyst.config import Settings, load_settings
from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.players import PlayerCache

logger = logging.getLogger(__name__)

PROJECTIONS_HOST = "https://api.sleeper.com"
FLEX_POSITIONS = {"RB", "WR", "TE"}
ZERO_CLUB_THRESHOLD = 2.0
LATE_ROUND_START = 9  # "drafted after round 8"


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
        p["player_id"]: (p["round"], p["pick_no"])
        for p in picks
        if p.get("player_id")
    }


def fetch_projections(season: str, week: int) -> dict[str, float]:
    """player_id -> projected PPR points. Empty dict if unavailable.

    The league is full PPR on otherwise-default scoring (rec 1.0, pass_td 4,
    pass_yd 0.04, rush/rec_yd 0.1), so Sleeper's precomputed pts_ppr lines up
    with how this league actually scores.
    """
    params = {
        "season_type": "regular",
        "position[]": ["QB", "RB", "WR", "TE", "K", "DEF"],
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


# -- award helpers ----------------------------------------------------------


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


def worst_start_sit(team: Team) -> tuple[Row, Row, float] | None:
    """The most embarrassing (started, benched, gap) pair at a compatible slot."""
    best: tuple[Row, Row, float] | None = None
    for sat in team.bench:
        for started in team.starters:
            slot = started.slot or ""
            compatible = started.pos == sat.pos or (
                slot.startswith("FLEX") and sat.pos in FLEX_POSITIONS
            )
            if not compatible:
                continue
            gap = sat.points - started.points
            if gap > 0 and (best is None or gap > best[2]):
                best = (started, sat, gap)
    return best


def fmt(points: float) -> str:
    return f"{points:.1f}"


def signed(points: float) -> str:
    return f"{points:+.1f}"


def who(row: Row) -> str:
    return f"**{row.name}** ({row.pos}, {row.nfl_team})"


def drafted_note(row: Row) -> str:
    if row.draft_round is None:
        return "undrafted"
    return f"round {row.draft_round}, pick {row.draft_pick} overall"


# -- the award show ---------------------------------------------------------


def render(
    teams: list[Team],
    roster_positions: list[str],
    week: int,
    have_proj: bool,
    have_draft: bool,
) -> str:
    out: list[str] = []
    add = out.append

    all_rows = [r for t in teams for r in t.rows]
    started_rows = [r for r in all_rows if r.started]
    by_score = sorted(teams, key=lambda t: t.points, reverse=True)
    champ, chump = by_score[0], by_score[-1]

    # Rosters are exclusive in a redraft league, so every player appears on at
    # most one team and each Row already names its owner. No dedupe needed.

    add(f"# 🏈 Week {week} Awards — Chace's 41st League")
    add("")
    add(
        f"Twelve teams, six games, and a *lot* of decisions that will not be repeated. "
        f"League average: **{fmt(sum(t.points for t in teams) / len(teams))}** points."
    )
    add("")

    # 1. Team of the Week
    add("## 🏆 Team of the Week")
    add("")
    top3 = sorted(champ.starters, key=lambda r: r.points, reverse=True)[:3]
    add(
        f"**{champ.name}** ({champ.manager}) — **{fmt(champ.points)}**, "
        f"beating {champ.opponent} by {fmt(abs(champ.margin))}."
    )
    add("")
    add("Carried by:")
    for r in top3:
        add(f"- {who(r)} — **{fmt(r.points)}** ({r.slot})")
    add("")

    # 3. Team of the Weak
    add("## 💀 Team of the Weak")
    add("")
    duds = sorted(chump.starters, key=lambda r: r.points)[:3]
    heroes = sorted(chump.starters, key=lambda r: r.points, reverse=True)[:2]
    add(
        f"**{chump.name}** ({chump.manager}) — **{fmt(chump.points)}**, "
        f"{fmt(champ.points - chump.points)} behind the league leader."
    )
    add("")
    add("The lowlights:")
    for r in duds:
        add(f"- {who(r)} — {fmt(r.points)} ({r.slot})")
    add("")
    add(
        "In fairness, "
        + " and ".join(f"{who(r)} ({fmt(r.points)})" for r in heroes)
        + " showed up. Everyone else filed a police report."
    )
    add("")

    if have_proj:
        # 2. Biggest overachiever among players somebody actually started
        add("## 📈 Biggest Overachiever")
        add("")
        beats = sorted(
            [r for r in started_rows if r.vs_proj is not None],
            key=lambda r: r.vs_proj or 0,
            reverse=True,
        )
        for r in beats[:3]:
            add(
                f"- {who(r)} — projected {fmt(r.projected or 0)}, scored "
                f"**{fmt(r.points)}** (**{signed(r.vs_proj or 0)}**). "
                f"Started by {r.team}."
            )
        add("")
        benched_beats = sorted(
            [r for r in all_rows if not r.started and r.vs_proj is not None],
            key=lambda r: r.vs_proj or 0,
            reverse=True,
        )
        if benched_beats:
            r = benched_beats[0]
            add(
                f"And the one nobody got to enjoy: {who(r)} beat his projection by "
                f"**{signed(r.vs_proj or 0)}** — from {r.team}'s bench."
            )
            add("")

        # 4. Biggest bust
        add("## 📉 Biggest Bust")
        add("")
        busts = sorted(
            [r for r in started_rows if r.vs_proj is not None],
            key=lambda r: r.vs_proj or 0,
        )
        for r in busts[:3]:
            add(
                f"- {who(r)} — projected {fmt(r.projected or 0)}, scored "
                f"**{fmt(r.points)}** (**{signed(r.vs_proj or 0)}**). "
                f"Started by {r.team}."
            )
        add("")

    if have_draft:

        def is_late(row: Row) -> bool:
            return row.draft_round is not None and row.draft_round >= LATE_ROUND_START

        # 5. Best late-round pick on any roster, started or not
        add(f"## 💎 Late-Round Steal (rounds {LATE_ROUND_START}+)")
        add("")
        late_any = sorted(
            [r for r in all_rows if is_late(r)], key=lambda r: r.points, reverse=True
        )
        for r in late_any[:3]:
            seat = (
                f"in {r.team}'s starting lineup"
                if r.started
                else f"on {r.team}'s bench"
            )
            add(
                f"- {who(r)} — **{fmt(r.points)}**, taken {drafted_note(r)}, "
                f"spent Sunday {seat}."
            )
        add("")

        late = [r for r in started_rows if is_late(r)]

        # 6. Best late-round pick someone actually started
        add("## 🎯 Late-Round Steal Somebody Actually Started")
        add("")
        started_late = sorted(late, key=lambda r: r.points, reverse=True)[:3]
        if started_late:
            for r in started_late:
                add(
                    f"- {who(r)} — **{fmt(r.points)}** from {drafted_note(r)}. "
                    f"Credit to {r.team} for having the nerve."
                )
        else:
            add("Nobody. Not one manager trusted a late-round pick. Cowards.")
        add("")

    # 7. Bench burner
    add("## 🔥 The Bench Burner")
    add("")
    blunders = [(t, worst_start_sit(t)) for t in teams]
    blunders = [(t, b) for t, b in blunders if b]
    blunders.sort(key=lambda tb: tb[1][2], reverse=True)
    for t, (started, sat, gap) in blunders[:3]:
        add(
            f"- **{t.name}** started {who(started)} for {fmt(started.points)} "
            f"while {who(sat)} put up **{fmt(sat.points)}** on the bench "
            f"(−{fmt(gap)})."
        )
    if blunders:
        t, (started, sat, gap) = blunders[0]
        if gap > abs(t.margin) and not t.won:
            add("")
            add(
                f"  ↳ Worth noting: {t.name} lost by {fmt(abs(t.margin))}. "
                f"That one decision was the whole game."
            )
    add("")

    # 8. Optimal lineup %
    add("## 🧠 Manager Efficiency")
    add("")
    eff = []
    for t in teams:
        best = optimal_lineup(t.rows, roster_positions)
        eff.append((t, best, (t.points / best * 100) if best else 0.0))
    eff.sort(key=lambda e: e[2], reverse=True)
    t, best, pct = eff[0]
    add(
        f"🥇 **Sharpest:** {t.name} — {fmt(t.points)} of a possible {fmt(best)} "
        f"(**{pct:.0f}%**)."
    )
    t, best, pct = eff[-1]
    add(
        f"🤕 **Roughest:** {t.name} — {fmt(t.points)} of a possible {fmt(best)} "
        f"(**{pct:.0f}%**). There was a much better team on that roster."
    )
    add("")
    add("| Team | Actual | Optimal | % |")
    add("| --- | ---: | ---: | ---: |")
    for t, best, pct in eff:
        add(f"| {t.name} | {fmt(t.points)} | {fmt(best)} | {pct:.0f}% |")
    add("")

    # 9. Luck
    add("## 😤 Luckiest & Unluckiest")
    add("")
    losers = [t for t in teams if not t.won and t.margin != 0]
    winners = [t for t in teams if t.won]
    unlucky = max(losers, key=lambda t: t.points) if losers else None
    if unlucky:
        add(
            f"**Unluckiest:** {unlucky.name} scored **{fmt(unlucky.points)}** — "
            f"more than {sum(1 for t in teams if t.points < unlucky.points)} other "
            f"teams — and still lost to {unlucky.opponent} "
            f"({fmt(unlucky.opponent_points)})."
        )
    if winners:
        lucky = min(winners, key=lambda t: t.points)
        add("")
        add(
            f"**Luckiest:** {lucky.name} won with just **{fmt(lucky.points)}**, "
            f"drawing {lucky.opponent} ({fmt(lucky.opponent_points)}). "
            f"A win is a win. Barely."
        )
    add("")

    # 10. Margins
    add("## ⚔️ Closest Game & Biggest Blowout")
    add("")
    # one entry per game, keyed so the two rosters collapse to a single row
    games: dict[tuple[str, ...], Team] = {}
    for t in teams:
        games.setdefault(tuple(sorted([t.name, t.opponent])), t)
    by_margin = sorted(games.values(), key=lambda t: abs(t.margin))
    closest, blowout = by_margin[0], by_margin[-1]

    def scoreline(t: Team) -> str:
        """Winner first, so it reads like a box score."""
        if t.won:
            return (
                f"{t.name} {fmt(t.points)} — {fmt(t.opponent_points)} {t.opponent} "
                f"(margin: {fmt(abs(t.margin))})"
            )
        return (
            f"{t.opponent} {fmt(t.opponent_points)} — {fmt(t.points)} {t.name} "
            f"(margin: {fmt(abs(t.margin))})"
        )

    add(f"**Nail-biter:** {scoreline(closest)}.")
    add("")
    add(f"**Beatdown:** {scoreline(blowout)}. Mercy rule, please.")
    add("")

    # 11. Zero club
    add(f"## 🥚 The Zero Club (started, scored ≤ {fmt(ZERO_CLUB_THRESHOLD)})")
    add("")
    zeros = sorted(
        [r for r in started_rows if r.points <= ZERO_CLUB_THRESHOLD],
        key=lambda r: (r.points, r.team),
    )
    if zeros:
        for r in zeros:
            add(f"- {r.name} ({r.pos}) — {fmt(r.points)} — started by {r.team}")
    else:
        add("Empty. Somehow every single starter in this league scored. Enjoy it.")
    add("")

    # 12. Undrafted hero
    if have_draft:
        add("## 🦄 Free Money (undrafted, and started anyway)")
        add("")
        undrafted = sorted(
            [r for r in started_rows if r.draft_round is None],
            key=lambda r: r.points,
            reverse=True,
        )
        if undrafted:
            for r in undrafted[:3]:
                add(
                    f"- {who(r)} — **{fmt(r.points)}** from a player nobody spent a "
                    f"pick on. {r.team} just picked him up off the floor."
                )
        else:
            add("Every starter in the league was drafted. A very boring, very safe week.")
        add("")

    # 13. League vs projections
    if have_proj:
        add("## 📊 The League vs. The Projections")
        add("")
        beat = [t for t in teams if t.projected is not None and t.points > t.projected]
        with_proj = [t for t in teams if t.projected is not None]
        if with_proj:
            add(
                f"**{len(beat)} of {len(with_proj)}** teams beat their projected total. "
                f"The other {len(with_proj) - len(beat)} of us are still calling it "
                f"'a process'."
            )
            add("")
            add("| Team | Projected | Actual | Diff |")
            add("| --- | ---: | ---: | ---: |")
            for t in sorted(
                with_proj, key=lambda t: t.points - (t.projected or 0), reverse=True
            ):
                add(
                    f"| {t.name} | {fmt(t.projected or 0)} | {fmt(t.points)} | "
                    f"{signed(t.points - (t.projected or 0))} |"
                )
            add("")

    # 14. Positional high scores
    add("## 🤡 Positional High Scores (started only)")
    add("")
    add("| Slot | Player | Points | Team |")
    add("| --- | --- | ---: | --- |")
    for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
        at_pos = [r for r in started_rows if r.pos == pos]
        if not at_pos:
            continue
        r = max(at_pos, key=lambda r: r.points)
        add(f"| {pos} | {r.name} | **{fmt(r.points)}** | {r.team} |")
    add("")

    # 15. Bulletin board
    add("## 🔮 Week 2 Bulletin Board")
    add("")
    add(f"- {chump.name} has nowhere to go but up. Statistically. Probably.")
    add(f"- {champ.name} peaked in week 1 and we all know it.")
    if blunders:
        add(
            f"- {blunders[0][0].name} is now the league's designated "
            f"'check your lineup' reminder."
        )
    add("")

    # Standup summary
    add("---")
    add("")
    add("## 📣 If you only read one thing")
    add("")
    add(f"1. **{champ.name}** dropped {fmt(champ.points)} and looked unbeatable.")
    add(f"2. **{chump.name}** managed {fmt(chump.points)} and looked unrecognizable.")
    if blunders:
        t, (started, sat, gap) = blunders[0]
        add(
            f"3. **{t.name}** left {fmt(gap)} points on the bench in a single slot — "
            f"the week's most expensive click."
        )
    if unlucky:
        add(
            f"4. Spare a thought for **{unlucky.name}**, who scored "
            f"{fmt(unlucky.points)} and lost anyway."
        )
    add("")

    notes = []
    if not have_proj:
        notes.append("projections were unavailable, so projection-based awards are skipped")
    if not have_draft:
        notes.append("draft data was unavailable, so draft-round awards are skipped")
    if notes:
        add(f"_Note: {'; '.join(notes)}._")
        add("")

    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: data/recap_week_N.md"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)

    with SleeperClient() as client:
        teams, roster_positions, have_proj, have_draft = build_teams(
            settings, client, args.week
        )

    markdown = render(teams, roster_positions, args.week, have_proj, have_draft)

    out_path = args.out or (settings.data_dir / f"recap_week_{args.week}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("Wrote %s", out_path)

    # The Windows console defaults to cp1252 and chokes on the emoji headers.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(markdown)


if __name__ == "__main__":
    main()
