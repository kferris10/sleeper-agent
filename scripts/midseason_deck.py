"""Render a midseason review deck: the latest week's highlights plus season-long slides.

Reuses league_deck.py's styling and four of its weekly slides (title is replaced,
high/low, wall of shame, positional high scores), then adds four season slides
built by running awards.build_teams over every completed week:

    standings (with all-play record and luck), season awards, storylines,
    season-long manager efficiency

Like league_deck.py, python-pptx is only needed here, so run it ephemerally:

    uv run --with python-pptx python scripts/midseason_deck.py [--week N]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

sys.path.insert(0, str(Path(__file__).resolve().parent))
import league_deck as deck  # noqa: E402
from league_deck import (  # noqa: E402
    FULL_W, GOLD, GREEN, MARGIN, MUTED, PANEL, RED, SLIDE_H, SLIDE_W, WHITE,
    footer, header, new_slide, rect, table, text,
)

from sleeper_analyst.awards import (  # noqa: E402
    LATE_ROUND_START,
    Awards,
    Team,
    build_teams,
    compute_awards,
    fmt,
    optimal_lineup,
)
from sleeper_analyst.config import load_settings  # noqa: E402
from sleeper_analyst.sleeper.client import SleeperClient  # noqa: E402

logger = logging.getLogger(__name__)


# -- season model -----------------------------------------------------------


@dataclass
class Record:
    """One team's season, accumulated week by week."""

    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    pf: float = 0.0
    pa: float = 0.0
    optimal: float = 0.0
    ap_wins: int = 0  # all-play: beat every team that scored less that week
    ap_losses: int = 0
    results: list[str] = field(default_factory=list)  # "W"/"L"/"T" in week order
    weekly_rank: list[int] = field(default_factory=list)

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def efficiency(self) -> float:
        return self.pf / self.optimal * 100 if self.optimal else 0.0

    @property
    def expected_wins(self) -> float:
        total = self.ap_wins + self.ap_losses
        return self.games * self.ap_wins / total if total else 0.0

    @property
    def luck(self) -> float:
        return self.wins - self.expected_wins

    @property
    def streak(self) -> str:
        if not self.results:
            return ""
        last, n = self.results[-1], 0
        for r in reversed(self.results):
            if r != last:
                break
            n += 1
        return f"{last}{n}"


@dataclass
class Card:
    label: str
    color: object
    title: str
    stat: str
    detail: str


@dataclass
class Season:
    through: int
    playoff_teams: int
    records: list[Record]  # standings order
    weeks: dict[int, list[Team]]
    awards_by_week: dict[int, Awards]
    drafted_by: dict[str, int]  # player_id -> roster_id that drafted him
    roster_positions: list[str]


def fetch_season(settings, through: int | None) -> tuple[Season, Awards]:
    weeks: dict[int, list[Team]] = {}
    awards_by_week: dict[int, Awards] = {}
    with SleeperClient() as client:
        if through is None:
            through = client.get_state().week - 1
        league = client.get_league(settings.league.league_id)
        playoff_teams = int(league.settings.get("playoff_teams", 4))
        drafts = client._get(f"/league/{settings.league.league_id}/drafts")
        picks = client._get(f"/draft/{drafts[0]['draft_id']}/picks") if drafts else []
        drafted_by = {p["player_id"]: p["roster_id"] for p in picks if p.get("player_id")}

        roster_positions: list[str] = []
        for wk in range(1, through + 1):
            teams, roster_positions, proj, draft, stats = build_teams(settings, client, wk)
            weeks[wk] = teams
            awards_by_week[wk] = compute_awards(teams, roster_positions, wk, proj, draft, stats)

    records: dict[int, Record] = {}
    for wk, teams in weeks.items():
        ranked = sorted(teams, key=lambda t: t.points, reverse=True)
        for t in teams:
            rec = records.setdefault(t.roster_id, Record(name=t.name))
            rec.name = t.name
            rec.pf += t.points
            rec.pa += t.opponent_points
            rec.optimal += optimal_lineup(t.rows, roster_positions)
            if t.points > t.opponent_points:
                rec.wins += 1
                rec.results.append("W")
            elif t.points < t.opponent_points:
                rec.losses += 1
                rec.results.append("L")
            else:
                rec.ties += 1
                rec.results.append("T")
            rec.ap_wins += sum(1 for o in teams if o.points < t.points)
            rec.ap_losses += sum(1 for o in teams if o.points > t.points)
            rec.weekly_rank.append(ranked.index(t) + 1)

    # Sleeper's default tiebreak is points for
    standings = sorted(records.values(), key=lambda r: (r.wins + 0.5 * r.ties, r.pf), reverse=True)
    season = Season(
        through=through,
        playoff_teams=playoff_teams,
        records=standings,
        weeks=weeks,
        awards_by_week=awards_by_week,
        drafted_by=drafted_by,
        roster_positions=roster_positions,
    )
    return season, awards_by_week[through]


# -- season math ------------------------------------------------------------


def season_awards(s: Season) -> list[Card]:
    started: dict[tuple[str, int], float] = defaultdict(float)  # (pid, roster) -> pts
    benched: dict[tuple[str, int], float] = defaultdict(float)
    rostered: dict[str, float] = defaultdict(float)  # pid -> pts on any roster
    info: dict[str, object] = {}
    team_of: dict[int, str] = {}
    for teams in s.weeks.values():
        for t in teams:
            team_of[t.roster_id] = t.name
            for r in t.rows:
                info[r.player_id] = r
                rostered[r.player_id] += r.points
                (started if r.started else benched)[(r.player_id, r.roster_id)] += r.points

    def label(pid: str) -> str:
        r = info[pid]
        return f"{r.name} ({r.pos}, {r.nfl_team})"

    cards: list[Card] = []

    (pid, rid), pts = max(started.items(), key=lambda kv: kv[1])
    cards.append(Card("🏅 Midseason MVP", GOLD, label(pid), fmt(pts),
                      f"Points started for {team_of[rid]}"))

    late = [(k, v) for k, v in started.items()
            if (info[k[0]].draft_round or 0) >= LATE_ROUND_START]
    if late:
        (pid, rid), pts = max(late, key=lambda kv: kv[1])
        r = info[pid]
        cards.append(Card("💎 Steal of the Draft", GREEN, label(pid), fmt(pts),
                          f"Round {r.draft_round}, pick {r.draft_pick} · {team_of[rid]}"))

    # undrafted only: "drafted by someone else" is mostly trades, not waivers
    pickups = [(k, v) for k, v in started.items() if k[0] not in s.drafted_by]
    if pickups:
        (pid, rid), pts = max(pickups, key=lambda kv: kv[1])
        cards.append(Card("📞 Waiver Wire Hall of Fame", GREEN, label(pid), fmt(pts),
                          f"Undrafted · points started for {team_of[rid]}"))

    firsts = [pid for pid, r in info.items() if r.draft_round == 1]
    if firsts:
        pid = min(firsts, key=lambda p: rostered[p])
        r = info[pid]
        drafter = team_of.get(s.drafted_by.get(pid, -1), "?")
        cards.append(Card("📉 First-Round Bust", RED, label(pid), fmt(rostered[pid]),
                          f"Pick {r.draft_pick} by {drafter} · season total"))

    (pid, rid), pts = max(benched.items(), key=lambda kv: kv[1])
    cards.append(Card("🪑 Bench Warmer of the Year", RED, label(pid), fmt(pts),
                      f"Points scored on {team_of[rid]}'s bench"))

    worst = max(s.records, key=lambda r: r.optimal - r.pf)
    cards.append(Card("🔥 Season Bench Burner", RED, worst.name,
                      fmt(worst.optimal - worst.pf),
                      f"Points left on the bench across {s.through} weeks"))
    return cards


def storylines(s: Season) -> list[Card]:
    cards: list[Card] = []
    luckiest = max(s.records, key=lambda r: r.luck)
    unluckiest = min(s.records, key=lambda r: r.luck)
    cards.append(Card("🍀 Luckiest Team", GREEN, luckiest.name, f"{luckiest.luck:+.1f} W",
                      f"{luckiest.wins}-{luckiest.losses} actual vs "
                      f"{luckiest.expected_wins:.1f} expected wins"))
    cards.append(Card("😤 Unluckiest Team", RED, unluckiest.name, f"{unluckiest.luck:+.1f} W",
                      f"{unluckiest.wins}-{unluckiest.losses} actual vs "
                      f"{unluckiest.expected_wins:.1f} expected wins"))

    tow = Counter(a.champ.name for a in s.awards_by_week.values() if a.champ)
    name, n = tow.most_common(1)[0]
    cards.append(Card("🏆 Trophy Case", GOLD, name, f"{n}×",
                      f"Team of the Week titles in {s.through} weeks"))

    best = max(((wk, t) for wk, ts in s.weeks.items() for t in ts), key=lambda x: x[1].points)
    cards.append(Card("🚀 Highest Score", GOLD, best[1].name, fmt(best[1].points),
                      f"Week {best[0]} vs {best[1].opponent}"))

    games = [(wk, t) for wk, ts in s.weeks.items() for t in ts if t.won]
    blowout = max(games, key=lambda x: x[1].margin)
    closest = min(games, key=lambda x: x[1].margin)
    # winner as the title, loser in the detail: "X over Y" wraps into the stat
    for lbl, color, (wk, t) in (("💥 Biggest Blowout", RED, blowout),
                                ("😬 Closest Game", GOLD, closest)):
        cards.append(Card(lbl, color, t.name, f"+{fmt(t.margin)}",
                          f"Wk {wk}: beat {t.opponent} "
                          f"{fmt(t.points)}–{fmt(t.opponent_points)}"))
    return cards


# -- slides -----------------------------------------------------------------


def card_grid(slide, cards: list[Card], top=Inches(2.3)):
    """Up to six award cards, three across and two down."""
    cols, gap = 3, Inches(0.25)
    width = int((FULL_W - gap * (cols - 1)) / cols)
    height = Inches(2.0)
    pad = Inches(0.25)
    inner = width - pad * 2
    for i, c in enumerate(cards[:6]):
        left = MARGIN + (width + gap) * (i % cols)
        y = top + (height + gap) * (i // cols)
        rect(slide, left, y, width, height, PANEL)
        text(slide, left + pad, y + Inches(0.18), inner, Inches(0.3),
             c.label.upper(), size=11, color=c.color, bold=True)
        text(slide, left + pad, y + Inches(0.48), inner, Inches(0.4),
             c.title, size=15, color=WHITE, bold=True)
        text(slide, left + pad, y + Inches(0.88), inner, Inches(0.6),
             c.stat, size=30, color=c.color, bold=True)
        text(slide, left + pad, y + Inches(1.5), inner, Inches(0.4),
             c.detail, size=11, color=MUTED)


def title_slide(prs: Presentation, s: Season):
    slide = new_slide(prs)
    rect(slide, Inches(0), Inches(0), SLIDE_W, Inches(0.16), GOLD)
    text(slide, MARGIN, Inches(2.1), FULL_W, Inches(0.5),
         f"WEEKS 1–{s.through}  ·  THE MIDSEASON REVIEW", size=18, color=GOLD, bold=True)
    text(slide, MARGIN, Inches(2.75), FULL_W, Inches(1.8),
         "Chace's 41st League", size=72, color=WHITE, bold=True, spacing=0.9)
    text(slide, MARGIN, Inches(4.5), Inches(10.5), Inches(1.0),
         f"{s.through} weeks down. {s.playoff_teams} playoff spots. "
         "Twelve teams who all still think they're in it.",
         size=20, color=MUTED)
    leader = s.records[0]
    text(slide, MARGIN, Inches(5.6), Inches(10.5), Inches(0.5),
         f"Leader at the turn: {leader.name} ({leader.wins}-{leader.losses}, "
         f"{fmt(leader.pf)} PF)", size=16, color=GOLD, bold=True)


def standings_slide(prs: Presentation, s: Season):
    slide = new_slide(prs)
    header(slide, "📊 Standings at the Turn", f"The top {s.playoff_teams} get in. Everyone else gets jokes.",
           "All-play: your record vs. every team, every week. Luck: wins above that pace.")
    rows = []
    for i, r in enumerate(s.records, start=1):
        record = f"{r.wins}-{r.losses}" + (f"-{r.ties}" if r.ties else "")
        rows.append([
            f"{i}. {r.name}", record, fmt(r.pf), fmt(r.pa),
            f"{r.ap_wins}-{r.ap_losses}", f"{r.luck:+.1f}", r.streak,
        ])
    highlight = {i: GOLD for i in range(s.playoff_teams)}
    table(
        slide,
        ["Team", "W-L", "PF", "PA", "All-play", "Luck", "Streak"],
        rows,
        top=Inches(2.95),
        widths=[4.1, 1.0, 1.25, 1.25, 1.4, 1.1, 1.1],
        highlight=highlight,
    )


def season_awards_slide(prs: Presentation, s: Season):
    slide = new_slide(prs)
    header(slide, "🎖️ Season Awards", f"The first {s.through} weeks, judged")
    card_grid(slide, season_awards(s))


def storylines_slide(prs: Presentation, s: Season):
    slide = new_slide(prs)
    header(slide, "📰 Storylines", "What everyone will be talking about")
    card_grid(slide, storylines(s))


def season_efficiency_slide(prs: Presentation, s: Season):
    slide = new_slide(prs)
    ranked = sorted(s.records, key=lambda r: r.efficiency, reverse=True)
    best, worst = ranked[0], ranked[-1]
    header(
        slide, "🧠 Season Manager Efficiency",
        f"Points scored vs. points available, weeks 1–{s.through}",
        f"Sharpest: {best.name} ({best.efficiency:.1f}%)  ·  "
        f"Roughest: {worst.name} ({worst.efficiency:.1f}%)",
    )
    table(
        slide,
        ["Team", "Actual", "Optimal", "Left on bench", "%"],
        [[r.name, fmt(r.pf), fmt(r.optimal), fmt(r.optimal - r.pf), f"{r.efficiency:.1f}%"]
         for r in ranked],
        top=Inches(2.85),
        widths=[4.6, 1.5, 1.5, 2.0, 1.2],
        highlight={0: GREEN, len(ranked) - 1: RED},
    )


def build_deck(s: Season, a: Awards) -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H

    title_slide(prs, s)
    deck.high_low_slide(prs, a)
    deck.wall_of_shame_slide(prs, a)
    deck.positional_slide(prs, a)
    standings_slide(prs, s)
    season_awards_slide(prs, s)
    storylines_slide(prs, s)
    season_efficiency_slide(prs, s)

    for i, slide in enumerate(list(prs.slides)[1:], start=2):
        footer(slide, a.week, i)
    return prs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, default=None,
                        help="last completed week to include (default: the most recent one)")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None,
                        help="default: data/midseason_week_N.pptx")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    season, latest = fetch_season(settings, args.week)

    out_path = args.out or (settings.data_dir / f"midseason_week_{season.through}.pptx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    build_deck(season, latest).save(str(out_path))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
