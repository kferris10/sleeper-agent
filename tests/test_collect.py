from sleeper_analyst.collect import (
    build_weekly_context,
    compute_free_agents,
    fantasy_positions,
    humanize_transaction,
    pair_matchups,
    slot_names,
)
from sleeper_analyst.sleeper.models import FreeAgent, Matchup, Player, Roster, Transaction
from conftest import UPCOMING_WEEK, fixture_json

# ---------------------------------------------------------------------------
# unit tests
# ---------------------------------------------------------------------------


def test_slot_names_numbers_repeated_positions():
    positions = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "WRRB_FLEX", "K", "DEF", "BN", "BN", "IR"]
    assert slot_names(positions) == [
        "QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLEX", "WRRB_FLEX", "K", "DEF",
    ]


def test_fantasy_positions_expands_flex():
    positions = fantasy_positions(["QB", "RB", "WRRB_FLEX", "K", "BN", "IR"])
    assert positions == {"QB", "RB", "WR", "K"}


def test_pair_matchups():
    matchups = [
        Matchup(roster_id=1, matchup_id=1),
        Matchup(roster_id=4, matchup_id=2),
        Matchup(roster_id=2, matchup_id=1),
        Matchup(roster_id=3, matchup_id=2),
        Matchup(roster_id=5, matchup_id=None),  # bye/median week entry
    ]
    pairs = pair_matchups(matchups)
    assert pairs == {1: 2, 2: 1, 3: 4, 4: 3}


def test_humanize_transaction_waiver_with_bid():
    tx = Transaction(
        transaction_id="1",
        type="waiver",
        status="complete",
        adds={"111": 3},
        drops={"222": 3},
        settings={"waiver_bid": 12},
        leg=6,
    )
    players = {
        "111": Player(player_id="111", full_name="Good Pickup", position="RB"),
        "222": Player(player_id="222", full_name="Roster Clogger", position="RB"),
    }
    text = humanize_transaction(tx, {3: "Team Three"}, players)
    assert text == "Waiver: added Good Pickup (Team Three); dropped Roster Clogger (Team Three) ($12 FAAB)"


def test_humanize_transaction_failed_flagged():
    tx = Transaction(transaction_id="2", type="waiver", status="failed", adds={"111": 1})
    players = {"111": Player(player_id="111", full_name="Missed Him", position="WR")}
    assert "[FAILED]" in humanize_transaction(tx, {}, players)


def test_compute_free_agents_excludes_rostered_and_ranks_by_trending():
    players = {
        "1": Player(player_id="1", full_name="Rostered Guy", position="RB", team="KC", active=True),
        "2": Player(player_id="2", full_name="Hot Pickup", position="RB", team="SF", active=True),
        "3": Player(player_id="3", full_name="Deep Stash", position="RB", team="DET", active=True, depth_chart_order=3),
        "4": Player(player_id="4", full_name="Retired Guy", position="RB", team=None, active=False),
        "5": Player(player_id="5", full_name="Punter Pete", position="P", team="KC", active=True),
    }
    rosters = [Roster(roster_id=1, players=["1"])]
    result = compute_free_agents(
        players, rosters, {"QB", "RB", "WR"}, trending_add={"3": 5, "2": 80}, trending_drop={},
        byes={"SF": 8, "DET": 6},
    )
    assert set(result) == {"RB"}
    agents = result["RB"]
    assert [a.name for a in agents] == ["Hot Pickup", "Deep Stash"]  # trending order
    assert agents[0].trending_add == 80
    assert agents[0].bye_week == 8
    assert all(isinstance(a, FreeAgent) for a in agents)


# ---------------------------------------------------------------------------
# fixture-based end-to-end collect
# ---------------------------------------------------------------------------


def test_build_weekly_context(client, settings):
    players = client.get_players()
    context = build_weekly_context(client, players, settings, week=UPCOMING_WEEK)

    assert context.season == "2025"
    assert context.completed_week == UPCOMING_WEEK - 1
    assert context.upcoming_week == UPCOMING_WEEK

    # league info
    assert context.league.waiver_type == "FAAB"
    assert context.league.num_teams == 12
    assert all(v != 0 for v in context.league.scoring_settings.values())

    # my team parses from the first fixture roster
    raw_roster = fixture_json("rosters")[0]
    assert context.my_team.roster_id == raw_roster["roster_id"]
    assert len(context.my_team.players) == len(raw_roster["players"])
    starters = [p for p in context.my_team.players if p.is_starter]
    assert len(starters) == len(raw_roster["starters"])
    ir = [p for p in context.my_team.players if p.is_ir]
    assert [p.id for p in ir] == raw_roster["reserve"]
    assert context.my_team.faab_remaining == 100 - raw_roster["settings"]["waiver_budget_used"]

    # matchup pairing
    assert context.last_matchup is not None
    slots = slot_names(fixture_json("league")["roster_positions"])
    assert [s.slot for s in context.last_matchup.my_starters] == slots
    assert context.last_matchup.my_points > 0
    starter_names = {s.player for s in context.last_matchup.my_starters}
    assert all(b.player not in starter_names for b in context.last_matchup.my_bench)

    assert context.upcoming_matchup is not None
    assert context.upcoming_matchup.opponent != ""
    assert [s.slot for s in context.upcoming_matchup.their_starters] == slots

    # other teams and standings
    assert len(context.other_teams) == 11
    assert all(t.positional_counts for t in context.other_teams)
    assert len(context.standings) == 12
    wins = [s.wins for s in context.standings]
    assert wins == sorted(wins, reverse=True)

    # free agents: never rostered, positions limited, byes enriched where known
    rostered = {pid for r in fixture_json("rosters") for pid in (r["players"] or [])}
    all_fas = [fa for agents in context.free_agents.values() for fa in agents]
    assert all_fas
    assert not rostered & {fa.id for fa in all_fas}
    assert set(context.free_agents) <= {"QB", "RB", "WR", "TE", "K", "DEF"}
    assert len(context.free_agents.get("RB", [])) <= 10
    assert any(fa.bye_week is not None for fa in all_fas)

    # bye enrichment on my roster
    for p in context.my_team.players:
        if p.nfl_team and p.nfl_team.upper() in settings.byes:
            assert p.bye_week == settings.byes[p.nfl_team.upper()]

    # activity is humanized
    assert context.league_activity
    assert all(item.description for item in context.league_activity)

    # context serializes cleanly
    assert context.model_dump_json()


def test_build_weekly_context_unknown_user(client, settings):
    players = client.get_players()
    settings.league.user_id = "does-not-exist"
    try:
        build_weekly_context(client, players, settings, week=UPCOMING_WEEK)
    except ValueError as exc:
        assert "No roster owned by" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown user_id")


def test_other_teams_include_full_rosters(client, settings):
    players = client.get_players()
    ctx = build_weekly_context(client, players, settings, week=UPCOMING_WEEK)
    assert ctx.other_teams
    for team in ctx.other_teams:
        assert team.players, f"{team.name} has no player list"
        assert all(p.name and p.id for p in team.players)
