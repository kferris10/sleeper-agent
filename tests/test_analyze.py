import json

import pytest

from conftest import UPCOMING_WEEK
from sleeper_analyst.analyze import (
    AnalysisError,
    extract_result,
    render_system,
    render_user,
    run_analysis,
    trim_free_agents,
    validate_analysis,
)
from sleeper_analyst.collect import build_weekly_context, slot_names
from sleeper_analyst.sleeper.models import WeeklyContext


@pytest.fixture
def context(client, settings) -> WeeklyContext:
    players = client.get_players()
    return build_weekly_context(client, players, settings, week=UPCOMING_WEEK)


def make_valid_result(context: WeeklyContext) -> dict:
    """Build a legal Analysis dict straight from the context."""
    slots = slot_names(context.league.roster_positions)
    starters = [p for p in context.my_team.players if p.is_starter]
    lineup = [
        {"slot": slot, "player_id": p.id, "player": p.name, "reason": "best option"}
        for slot, p in zip(slots, starters)
    ]
    fa = next(iter(context.free_agents.values()))[0]
    bench_player = next(p for p in context.my_team.players if not p.is_starter and not p.is_ir)
    return {
        "recap": {"summary": "Solid week.", "what_worked": ["QB"], "what_hurt": [], "self_grade": "B"},
        "lineup": lineup,
        "bench": [{"player": bench_player.name, "player_id": bench_player.id, "reason": "tough matchup"}],
        "waivers": [
            {
                "priority": 1,
                "add_player_id": fa.id,
                "add": fa.name,
                "drop_player_id": bench_player.id,
                "drop": bench_player.name,
                "faab_bid": None,
                "reason": "upside",
            }
        ],
        "trades": [],
        "watchlist": [{"player": fa.name, "note": "monitor usage"}],
        "confidence_notes": "weather could change things",
    }


def wrap(result: dict) -> str:
    return f"<analysis>thinking...</analysis>\n<result>\n{json.dumps(result)}\n</result>"


# -- extract_result ----------------------------------------------------------


def test_extract_result_parses_json():
    assert extract_result("<analysis>x</analysis><result>{\"a\": 1}</result>") == {"a": 1}


def test_extract_result_missing_block():
    with pytest.raises(AnalysisError, match="no <result>"):
        extract_result("just some text")


def test_extract_result_bad_json():
    with pytest.raises(AnalysisError, match="not valid JSON"):
        extract_result("<result>{oops}</result>")


# -- validate_analysis -------------------------------------------------------


def test_valid_analysis_passes(context):
    analysis = validate_analysis(make_valid_result(context), context)
    assert len(analysis.lineup) == len(slot_names(context.league.roster_positions))
    assert analysis.waivers[0].priority == 1


def test_hallucinated_lineup_player_rejected(context):
    result = make_valid_result(context)
    result["lineup"][0]["player_id"] = "9999999"
    with pytest.raises(AnalysisError, match="not on my roster"):
        validate_analysis(result, context)


def test_waiver_add_must_be_free_agent(context):
    result = make_valid_result(context)
    result["waivers"][0]["add_player_id"] = context.my_team.players[0].id  # rostered, not FA
    with pytest.raises(AnalysisError, match="not in the free-agent list"):
        validate_analysis(result, context)


def test_waiver_drop_must_be_rostered(context):
    result = make_valid_result(context)
    result["waivers"][0]["drop_player_id"] = "424242"
    with pytest.raises(AnalysisError, match="not on my roster"):
        validate_analysis(result, context)


def test_missing_slot_rejected(context):
    result = make_valid_result(context)
    result["lineup"] = result["lineup"][1:]  # drop a slot
    with pytest.raises(AnalysisError, match="lineup slots"):
        validate_analysis(result, context)


def test_faab_overbid_rejected(context):
    result = make_valid_result(context)
    context.my_team.faab_remaining = 10
    result["waivers"][0]["faab_bid"] = 50
    with pytest.raises(AnalysisError, match="exceeds remaining budget"):
        validate_analysis(result, context)


def test_schema_violation_rejected(context):
    with pytest.raises(AnalysisError, match="schema"):
        validate_analysis({"recap": {"summary": "x"}}, context)  # lineup missing


# -- prompt rendering --------------------------------------------------------


def test_render_system_includes_philosophy_and_rules(settings, context):
    settings.philosophy.notes = "Primary goal: win the league."
    text = render_system(settings, context)
    assert "win the league" in text
    assert "waiver priority" not in text.lower() or context.league.waiver_type != "FAAB"
    assert "<result>" in text  # output contract present
    for slot in slot_names(context.league.roster_positions):
        assert slot in text


def test_render_user_includes_ids_and_tables(context):
    text = render_user(context)
    for p in context.my_team.players:
        assert p.id in text
    assert context.upcoming_matchup.opponent in text
    assert "Free agents" in text


def test_trim_free_agents_halves_lists(context):
    trimmed = trim_free_agents(context, keep_fraction=0.5)
    for pos, agents in context.free_agents.items():
        assert len(trimmed.free_agents[pos]) <= max(2, len(agents) // 2 + 1)
    # original untouched
    assert sum(len(v) for v in context.free_agents.values()) > sum(
        len(v) for v in trimmed.free_agents.values()
    )


# -- run_analysis retry loop -------------------------------------------------


def test_run_analysis_happy_path(context, settings):
    calls = []

    def generate(system: str, user: str) -> str:
        calls.append(user)
        return wrap(make_valid_result(context))

    analysis, raw = run_analysis(context, settings, generate=generate)
    assert len(calls) == 1
    assert "<result>" in raw
    assert analysis.recap.summary == "Solid week."


def test_run_analysis_retries_once_on_bad_output(context, settings):
    bad = make_valid_result(context)
    bad["lineup"][0]["player_id"] = "hallucinated"
    responses = [wrap(bad), wrap(make_valid_result(context))]
    calls = []

    def generate(system: str, user: str) -> str:
        calls.append(user)
        return responses[len(calls) - 1]

    analysis, _ = run_analysis(context, settings, generate=generate)
    assert len(calls) == 2
    assert "failed validation" in calls[1]  # corrective prompt includes the errors
    assert analysis.lineup[0].player_id != "hallucinated"


def test_run_analysis_fails_after_two_bad_attempts(context, settings):
    bad = make_valid_result(context)
    bad["lineup"][0]["player_id"] = "hallucinated"

    def generate(system: str, user: str) -> str:
        return wrap(bad)

    with pytest.raises(AnalysisError):
        run_analysis(context, settings, generate=generate)
