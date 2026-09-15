import pytest

from conftest import UPCOMING_WEEK
from test_analyze import make_valid_result
from sleeper_analyst.collect import build_weekly_context
from sleeper_analyst.report import (
    lineup_changes,
    render_html,
    render_markdown,
    subject_line,
)
from sleeper_analyst.sleeper.models import Analysis, WeeklyContext


@pytest.fixture
def context(client, settings) -> WeeklyContext:
    players = client.get_players()
    return build_weekly_context(client, players, settings, week=UPCOMING_WEEK)


@pytest.fixture
def analysis(context) -> Analysis:
    result = make_valid_result(context)
    result["trades"] = [
        {
            "partner": "Rival Team",
            "give": ["Player A", "Player B"],
            "get": ["Star Player"],
            "pitch": "You need depth & I need a star <now>.",
            "why_it_helps_me": "Consolidates my lineup.",
            "why_they_might_accept": "They are thin at two spots.",
        }
    ]
    return Analysis.model_validate(result)


def test_subject_line_names_week_and_league(context):
    subject = subject_line(context)
    assert f"Week {context.upcoming_week}" in subject
    assert context.league.name in subject
    assert "Friday" not in subject
    assert "Friday injury update" in subject_line(context, tag="friday")


def test_markdown_actions_first_then_breakdown(analysis, context):
    md = render_markdown(analysis, context)
    assert "do these in the Sleeper app" in md
    assert "## 1. Set this lineup" in md
    for rec in analysis.lineup:
        assert rec.player in md
    assert "Submit these waiver claims" in md
    assert f"- [ ] 1. Add **{analysis.waivers[0].add}**" in md
    assert "Send these trade offers" in md
    assert "> You need depth" in md  # paste-ready pitch
    # the full breakdown follows the action list
    fold = md.index("# Full breakdown")
    assert md.index("Set this lineup") < fold
    assert md.index(analysis.recap.summary) > fold
    assert md.index(analysis.lineup[0].reason) > fold
    assert md.index(analysis.confidence_notes) > fold
    assert md.index("Watchlist") > fold


def test_markdown_summarizes_changes(analysis, context):
    # make_valid_result mirrors current starters → no change markers
    md = render_markdown(analysis, context)
    assert "No changes" in md and "← change" not in md
    # pretend the current starters are all different → every row flagged
    for p in context.my_team.players:
        p.is_starter = False
    md = render_markdown(analysis, context)
    assert md.count("← change") == len(analysis.lineup)
    assert "Changes: " in md
    assert len(lineup_changes(analysis, context)) == len(analysis.lineup)


def test_html_escapes_and_renders(analysis, context):
    html = render_html(analysis, context)
    assert "&amp;" in html and "&lt;now&gt;" in html  # pitch text is escaped
    assert "<script" not in html
    for rec in analysis.lineup:
        assert rec.player in html
    assert "waiver claims" in html
    # no change rows → no highlight style
    assert "background:#fff3cd" not in html


def test_html_highlights_changes(analysis, context):
    for p in context.my_team.players:
        p.is_starter = False
    html = render_html(analysis, context)
    assert html.count("&larr; change") == len(analysis.lineup)


# -- awards section ---------------------------------------------------------


def test_packet_omits_awards_when_none(analysis, context):
    markdown = render_markdown(analysis, context)
    html = render_html(analysis, context)
    assert "League awards" not in markdown
    assert "League awards" not in html


def test_packet_appends_awards_below_the_decisions(analysis, context):
    from test_awards import ROSTER_POSITIONS, make_team

    from sleeper_analyst.awards import compute_awards

    teams = [
        make_team(1, "Alpha", [10.0] * 9, "Bravo", 45.0),
        make_team(2, "Bravo", [5.0] * 9, "Alpha", 90.0),
    ]
    awards = compute_awards(teams, ROSTER_POSITIONS, week=context.completed_week,
                            have_projections=False, have_draft=False)

    markdown = render_markdown(analysis, context, awards)
    html = render_html(analysis, context, awards)

    # the actionable packet still leads; awards come after it
    assert markdown.index("Set this lineup") < markdown.index("League awards")
    assert html.index("Set this lineup") < html.index("League awards")
    assert "Team of the Week" in markdown
    assert "Team of the Week" in html
    # the awards must sit inside the single-column wrapper, not after it
    assert html.rstrip().endswith("</div>")
    assert html.count("<div") == html.count("</div>")
