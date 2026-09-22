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

uv run python scripts/league_recap.py [--week N]                  # award show as markdown
uv run --with python-pptx python scripts/league_deck.py [--week N]  # award show as a deck
```

## Where the state lives

- `config.toml` — league ids and the management philosophy (goals: 1. win the league,
  2. don't finish last; the philosophy knobs feed the analysis prompt directly)
- `data/context_week_N.json` — full league snapshot: my roster, every rival roster,
  standings, free agents, recent transactions
- `data/analysis_week_N.json` — the latest decision packet with all reasoning
- `data/history.db` — SQLite history of contexts/analyses/deliveries
- `data/recap_week_N.md` — the league award show (also appended to the Tuesday email)
- `data/awards_week_N.pptx` — the same awards as a deck; gitignored

## League awards

`src/sleeper_analyst/awards.py` computes all 16 award categories for the whole
league from one `compute_awards`; `report.py` renders them to markdown and
email-safe HTML, and `scripts/league_deck.py` renders them to PowerPoint. The
Tuesday email carries them, the Friday email does not (`_load_awards` in `cli.py`
short-circuits on `tag == "friday"` before any network call).

Three of its four data sources are **unofficial** and must stay failure-tolerant —
draft picks, and projections and box-score stats from `api.sleeper.com` (a different
host than the v1 API the client wraps; `_fetch_weekly` wraps both of the latter).
Awards are the fun half of the packet; they are never allowed to cost the owner the
actionable half, so every failure degrades to "no awards section".

The deck's standup cut closes with the positional high scores and `last_word_slide`,
which pairs "if you only read one thing" with the week N+1 bulletin board. Those
bulletin jabs live in two places — `bulletin_lines` in `league_deck.py` and the
markdown/HTML renderers in `report.py` — so change both or the deck and the email
start telling different jokes.

The **wall of shame** (`compute_shame`) is the one category built from the box
scores: a started RB/WR/TE with zero carries and zero catches needs usage, which
the matchup endpoint does not carry. A player with *no* line in the stats feed is
never accused — an absent entry means the feed did not know him, not that he did
nothing. Each check is capped (default two entries per category) so one wide
category cannot fill the whole wall, and the deck shows the first
`SHAME_SLIDE_ROWS`.

Note the league is redraft with exclusive rosters, so every started player is
started by exactly one team — "most-started player" awards are meaningless here.

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
6. Remember the trade deadline is week 10. This league does **not** trade draft picks
   (redraft, no keepers) — never offer or ask for one. Sweeten with players only, and
   prefer even 2-for-2 shapes: both rosters sit at the 16-man limit with no IR slot, so
   an uneven swap forces the short side to go stream a free agent, which managers resist.

Kevin executes accepted trades in the Sleeper app himself; nothing here writes to Sleeper.

## Conventions

- Python 3.11+ via uv, pydantic v2 models in `src/sleeper_analyst/sleeper/models.py`
- Tests are offline against `tests/fixtures/` (re-record with `tests/record_fixtures.py`)
- Secrets come from env/.env (`ANTHROPIC_API_KEY`, `SMTP_PASSWORD`) — never in config.toml
- Windows dev machine: read files with `encoding="utf-8-sig"` tolerance, beware
  PowerShell 5.1 BOM/encoding quirks
- The weekly workflow commits `data/` back to the repo — pull before local runs

## Keep the docs current

This repo is written entirely by Claude, so the docs are the only handoff between
sessions — a stale doc actively misleads the next one. Treat them as part of the
change, not a follow-up, and update them in the same turn as the code:

- **`README.md`** — how a human runs it. Update when a command, flag, output path,
  or schedule changes.
- **`CLAUDE.md`** (this file) — how Claude works on it. Update when a module's role,
  a data source, or a convention changes. Record the *non-obvious* things: traps,
  why a thing is the way it is, facts that took a live API call to learn.
- **`plans/PROJECT_PLAN.md`** — the design. Update the repo layout, endpoint tables,
  and data flow when they change. Mark unofficial/undocumented endpoints as such.

Before finishing a change, sweep for staleness rather than assuming:

```sh
grep -rn "OLD_NAME" --include="*.py" --include="*.md" . | grep -v __pycache__
```

Check that every command shown in a doc still runs as written and every path it
names still exists. Renaming a file means updating its docstring, both other docs,
and any sibling script that mentions it.
