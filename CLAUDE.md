# sleeper-analyst

Weekly Sleeper fantasy football analyst for Kevin's team ("NotASmurf") in Chace's 41st
League (12 teams, waiver **priority** not FAAB, trade deadline week 10, playoffs from
week 14). GitHub Actions runs the pipeline Tuesday and Friday mornings and emails a
decision packet; Kevin executes the moves in the Sleeper app. This team is managed
entirely by Claude — recommendations are decisions, not suggestions.

## Commands

```sh
uv run pytest                             # offline tests (fixtures, no network)
uv run sleeper-analyst collect            # refresh data/context_week_N.json from the Sleeper API
uv run sleeper-analyst analyze            # Claude analysis of the newest context (costs ~$1)
uv run sleeper-analyst deliver --dry-run  # render packet without emailing
uv run sleeper-analyst run [--tag friday] # full pipeline (idempotent per week+tag)
```

## Where the state lives

- `config.toml` — league ids and the management philosophy (goals: 1. win the league,
  2. don't finish last; the philosophy knobs feed the analysis prompt directly)
- `data/context_week_N.json` — full league snapshot: my roster, every rival roster,
  standings, free agents, recent transactions
- `data/analysis_week_N.json` — the latest decision packet with all reasoning
- `data/history.db` — SQLite history of contexts/analyses/deliveries

## Trade discussions (owner pastes a message from another manager)

When Kevin pastes a trade offer, counter, or trash-talk-with-an-offer-inside from a
league member, act as the team's GM and produce a reply he can paste back. Do this:

1. Read the newest `data/context_week_N.json` for both rosters (rival rosters are in
   `other_teams[].players`), standings, and my bye-week exposure. If it looks stale
   (older than this week), run `uv run sleeper-analyst collect` first.
2. Read the newest `data/analysis_week_N.json` — it may already contain a trade stance
   or pitch involving this team.
3. Evaluate strictly by the config philosophy: championship equity first, 2-for-1
   consolidations that upgrade the starting lineup, buy low on injured stars when the
   weeks 14–17 playoff timeline works, protect RB depth through the weeks 10–11 byes.
4. Use web search for current injury/role news on every player in the proposed deal
   before judging value.
5. Answer with: **verdict** (accept / counter / decline), the reasoning in a few
   sentences, and a **paste-ready reply message** written in Kevin's casual voice.
   Counters must name specific players from the other team's roster (options are fine:
   "Achane or Barkley") — never a vague "one of your RBs".
6. Remember the trade deadline is week 10 and the league trades draft picks
   (`traded_picks` endpoint) — picks can sweeten a deal.

Kevin executes accepted trades in the Sleeper app himself; nothing here writes to Sleeper.

## Conventions

- Python 3.11+ via uv, pydantic v2 models in `src/sleeper_analyst/sleeper/models.py`
- Tests are offline against `tests/fixtures/` (re-record with `tests/record_fixtures.py`)
- Secrets come from env/.env (`ANTHROPIC_API_KEY`, `SMTP_PASSWORD`) — never in config.toml
- Windows dev machine: read files with `encoding="utf-8-sig"` tolerance, beware
  PowerShell 5.1 BOM/encoding quirks
- The weekly workflow commits `data/` back to the repo — pull before local runs
