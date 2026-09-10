import pytest

from conftest import UPCOMING_WEEK
from test_analyze import make_valid_result
from sleeper_analyst.collect import build_weekly_context
from sleeper_analyst.report import render_html, render_markdown, subject_line
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


def test_markdown_contains_all_sections(analysis, context):
    md = render_markdown(analysis, context)
    assert f"# Week {context.upcoming_week} decision packet" in md
    for rec in analysis.lineup:
        assert rec.player in md
    assert "- [ ] 1. Add" in md  # waiver checklist
    assert "### To Rival Team" in md
    assert "> You need depth" in md  # paste-ready pitch as blockquote
    assert analysis.watchlist[0].player in md
    assert analysis.confidence_notes in md


def test_markdown_flags_lineup_changes(analysis, context):
    # make_valid_result mirrors current starters → no change markers
    assert "← change" not in render_markdown(analysis, context)
    # pretend the current starters are all different → every row flagged
    for p in context.my_team.players:
        p.is_starter = False
    md = render_markdown(analysis, context)
    assert md.count("← change") == len(analysis.lineup)


def test_html_escapes_and_renders(analysis, context):
    html = render_html(analysis, context)
    assert "&amp;" in html and "&lt;now&gt;" in html  # pitch text is escaped
    assert "<script" not in html
    for rec in analysis.lineup:
        assert rec.player in html
    assert "Waiver claims" in html
    # no change rows → no highlight style
    assert "background:#fff3cd" not in html


def test_html_highlights_changes(analysis, context):
    for p in context.my_team.players:
        p.is_starter = False
    html = render_html(analysis, context)
    assert html.count("&larr; change") == len(analysis.lineup)
