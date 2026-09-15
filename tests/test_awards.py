"""Offline tests for the league award math and its two renderers.

These build Teams/Rows directly rather than going through build_teams: the
projections endpoint is a bare httpx.get against a different host, so it is not
covered by the MockTransport the other tests use, and the award math is the part
worth pinning anyway.
"""

import pytest

from sleeper_analyst.awards import (
    Row,
    Team,
    compute_awards,
    optimal_lineup,
    worst_start_sit,
)
from sleeper_analyst.report import render_awards_html, render_awards_markdown

ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 7
SLOTS = ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLEX", "K", "DEF"]
STARTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "WR", "K", "DEF"]


def make_row(team: str, name: str, pos: str, points: float, **kw) -> Row:
    return Row(
        roster_id=kw.get("roster_id", 1),
        team=team,
        player_id=kw.get("player_id", f"{team}-{name}"),
        name=name,
        pos=pos,
        nfl_team=kw.get("nfl_team", "NE"),
        points=points,
        projected=kw.get("projected"),
        started=kw.get("started", True),
        slot=kw.get("slot"),
        draft_round=kw.get("draft_round"),
        draft_pick=kw.get("draft_pick"),
    )


def make_team(
    roster_id: int,
    name: str,
    starter_points: list[float],
    opponent: str,
    opponent_points: float,
    bench: list[Row] | None = None,
) -> Team:
    rows = [
        make_row(
            name, f"{name} {slot}", pos, pts,
            roster_id=roster_id, started=True, slot=slot,
            projected=10.0, draft_round=1, draft_pick=roster_id,
        )
        for slot, pos, pts in zip(SLOTS, STARTER_POSITIONS, starter_points)
    ]
    for row in bench or []:
        row.roster_id = roster_id
        row.team = name
        rows.append(row)
    return Team(
        roster_id=roster_id,
        name=name,
        manager=f"{name}-mgr",
        points=sum(starter_points),
        opponent=opponent,
        opponent_points=opponent_points,
        rows=rows,
    )


@pytest.fixture
def teams() -> list[Team]:
    """Two games. Alpha blows out Bravo; Delta edges Charlie."""
    big = [30.0, 25.0, 20.0, 18.0, 15.0, 12.0, 10.0, 9.0, 8.0]  # 147.0
    small = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]  # 45.0
    mid = [12.0, 11.0, 10.0, 10.0, 9.0, 9.0, 8.0, 8.0, 7.0]  # 84.0
    mid2 = [13.0, 11.0, 10.0, 10.0, 9.0, 9.0, 8.0, 8.0, 7.0]  # 85.0

    alpha = make_team(1, "Alpha", big, "Bravo", 45.0)
    bravo = make_team(
        2, "Bravo", small, "Alpha", 147.0,
        # a benched TE who massively outscored the started one
        bench=[make_row("Bravo", "Bench Star", "TE", 40.0, started=False, draft_round=12,
                        draft_pick=140, projected=5.0)],
    )
    charlie = make_team(3, "Charlie", mid, "Delta", 85.0)
    delta = make_team(
        4, "Delta", mid2, "Charlie", 84.0,
        bench=[make_row("Delta", "Waiver Guy", "WR", 3.0, started=False, projected=2.0)],
    )
    return [alpha, bravo, charlie, delta]


@pytest.fixture
def awards(teams):
    return compute_awards(teams, ROSTER_POSITIONS, week=3,
                          have_projections=True, have_draft=True)


def test_champ_and_chump_are_the_score_extremes(awards):
    assert awards.champ.name == "Alpha"
    assert awards.chump.name == "Bravo"
    assert awards.league_average == pytest.approx((147.0 + 45.0 + 84.0 + 85.0) / 4)


def test_champ_top_is_highest_scoring_starters(awards):
    assert [r.points for r in awards.champ_top] == [30.0, 25.0, 20.0]


def test_bench_burner_finds_the_benched_star(awards):
    top = awards.blunders[0]
    assert top.team.name == "Bravo"
    assert top.benched.name == "Bench Star"
    assert top.gap == pytest.approx(40.0 - 6.0)  # bench TE vs. started TE


def test_worst_start_sit_ignores_incompatible_positions():
    team = make_team(
        9, "Solo", [5.0] * 9, "Nobody", 0.0,
        bench=[make_row("Solo", "Backup K", "K", 99.0, started=False)],
    )
    blunder = worst_start_sit(team)
    # the started K scored 8.0 (index 7), so a 99-point bench K is the only swap
    assert blunder is not None
    assert blunder.benched.name == "Backup K"
    assert blunder.started.pos == "K"


def test_optimal_lineup_cascades_the_displaced_starter_into_flex(teams):
    bravo = next(t for t in teams if t.name == "Bravo")
    best = optimal_lineup(bravo.rows, ROSTER_POSITIONS)
    # Started 1..9 by slot (TE=6.0, FLEX=WR 7.0, weakest WR=4.0) for 45.0 total.
    # The 40.0 bench TE takes the TE slot, which pushes the 6.0 TE into FLEX and
    # drops the 4.0 WR entirely -- so the gain is +36, not just the +34 at TE.
    assert best == pytest.approx(81.0)
    assert best == pytest.approx(bravo.points + 36.0)


def test_efficiency_is_sorted_best_first(awards):
    pcts = [e.pct for e in awards.efficiency]
    assert pcts == sorted(pcts, reverse=True)
    assert awards.efficiency[-1].team.name == "Bravo"  # the one who benched a 40


def test_luck_and_margins(awards):
    assert awards.blowout.name in ("Alpha", "Bravo")
    assert abs(awards.blowout.margin) == pytest.approx(102.0)
    assert abs(awards.closest.margin) == pytest.approx(1.0)
    # Charlie scored 84 and lost by 1; Bravo scored 45 and lost by 102
    assert awards.unlucky.name == "Charlie"
    assert awards.lucky.name == "Delta"


def test_zero_club_catches_low_starters(awards):
    names = {r.name for r in awards.zero_club}
    assert "Bravo QB" in names  # 1.0
    assert "Bravo RB1" in names  # 2.0
    assert "Alpha QB" not in names


def test_undrafted_only_includes_started_players(awards):
    # "Waiver Guy" is undrafted but benched, so he is not Free Money
    assert all(r.started for r in awards.undrafted)
    assert "Waiver Guy" not in {r.name for r in awards.undrafted}


def test_positional_best_covers_every_started_slot(awards):
    positions = [pos for pos, _ in awards.positional_best]
    assert positions == ["QB", "RB", "WR", "TE", "K", "DEF"]
    qb = dict(awards.positional_best)["QB"]
    assert qb.points == 30.0


# -- rendering --------------------------------------------------------------

ALL_CATEGORIES = [
    "Team of the Week",
    "Team of the Weak",
    "Biggest Overachiever",
    "Biggest Bust",
    "Late-Round Steal",
    "Bench Burner",
    "Manager Efficiency",
    "Luckiest",
    "Closest Game",
    "Zero Club",
    "Free Money",
    "The Projections",
    "Positional High Scores",
    "Bulletin Board",
    "If you only read one thing",
]


def test_markdown_renders_every_category(awards):
    md = render_awards_markdown(awards)
    for category in ALL_CATEGORIES:
        assert category in md, category
    assert "| Team | Actual | Optimal | % |" in md


def test_html_renders_every_category_and_escapes(awards):
    awards.champ.name = "Alpha & <Co>"
    html = render_awards_html(awards)
    for category in ALL_CATEGORIES:
        plain = category.replace("&", "&amp;")
        assert plain in html or category in html, category
    assert "Alpha &amp; &lt;Co&gt;" in html
    assert "<script" not in html


def test_renderers_degrade_without_projections_or_draft(teams):
    awards = compute_awards(teams, ROSTER_POSITIONS, week=3,
                            have_projections=False, have_draft=False)
    md = render_awards_markdown(awards)
    html = render_awards_html(awards)
    for gone in ["Biggest Overachiever", "Biggest Bust", "Late-Round Steal", "Free Money"]:
        assert gone not in md
        assert gone not in html
    # the categories that do not need those feeds still render
    assert "Team of the Week" in md
    assert "Zero Club" in md
    assert "projections were unavailable" in md
    assert "draft data was unavailable" in md


def test_intro_counts_the_actual_league_size(awards):
    assert render_awards_markdown(awards).startswith("4 teams, 2 games")


# -- when the packet carries awards at all -----------------------------------


def test_friday_run_skips_awards(settings):
    """The Friday email is a lean injury re-check, not a second award show.

    This must short-circuit before any network call -- the test would hit the
    live projections host otherwise, since that request bypasses MockTransport.
    """
    from sleeper_analyst.cli import _load_awards

    assert _load_awards(settings, 6, "friday") is None


def test_preseason_has_no_completed_week_to_award(settings):
    from sleeper_analyst.cli import _load_awards

    assert _load_awards(settings, 0) is None
