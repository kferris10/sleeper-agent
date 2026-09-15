# sleeper-analyst

Weekly Sleeper fantasy football analyst. Every Tuesday it pulls league state from the
Sleeper API, has Claude analyze it, and delivers a decision packet (recap, lineup,
waivers, trades) you execute in the Sleeper app, followed by a league-wide award
show for last week. Friday adds a lean injury re-check. Full design:
`plans/PROJECT_PLAN.md`.

**Status: Phase 3** — full pipeline: `run` chains collect → Claude analysis → email
delivery, scheduled Tuesday and Friday mornings via GitHub Actions
(`.github/workflows/weekly.yml`).

> **This repository is AI-generated.** Every line of code, test, and document here
> was written by Claude (via [Claude Code](https://claude.com/claude-code)), and the
> fantasy team it manages is run on Claude's recommendations rather than the owner's.
> Read it with that in mind: it has not had a human code review. Two of the Sleeper
> endpoints it depends on are undocumented and may change without notice
> (see [League awards](#league-awards)).

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it provisions Python 3.11+ automatically).

```sh
uv sync
```

Find your Sleeper ids and create your config:

```sh
uv run sleeper-analyst setup --username YOUR_SLEEPER_NAME
# copy config.example.toml -> config.toml, fill in [league] league_id and user_id
```

## Collect

```sh
uv run sleeper-analyst collect            # upcoming week from /state/nfl
uv run sleeper-analyst collect --week 3   # specific week
```

Writes `data/context_week_N.json` — the complete `WeeklyContext` (your roster, last/upcoming
matchups, other teams, ranked free agents, recent transactions, standings) that Phase 2
feeds to Claude. The NFL player map is cached at `data/cache/players.json` with a 24h TTL
and is never refetched sooner, per Sleeper's API guidance.

Read the JSON top to bottom and check it against what you see in the Sleeper app — that's
the Phase 1 acceptance test.

## Analyze + deliver

```sh
uv run sleeper-analyst analyze              # Claude on the newest saved context
uv run sleeper-analyst deliver --dry-run    # render packet_week_N.md/.html only
uv run sleeper-analyst deliver              # render + send via [delivery] channel
uv run sleeper-analyst run                  # the whole weekly pipeline
```

`run` is idempotent per (week, tag): once a week's packet is delivered it exits early
unless you pass `--force`. Any exception sends a short "run FAILED" alert through
the same delivery channel.

## League awards

The Tuesday packet ends with 15 award categories covering the whole league —
team of the week, biggest bust vs. projection, points left on the bench, manager
efficiency, the zero club, late-round steals. The Friday email skips them: it
covers the same completed week Tuesday already reported.

The same numbers render two other ways, both standalone (neither touches the
pipeline, so a broken script can't affect the scheduled run):

```sh
uv run python scripts/league_recap.py                     # newest completed week
uv run --with python-pptx python scripts/league_deck.py   # 7-slide standup cut
uv run --with python-pptx python scripts/league_deck.py --week 3 --full  # a past week, all 16 slides
```

The deck defaults to a 7-slide standup cut; `--full` gives one slide per award.
`python-pptx` is deliberately not a project dependency — `uv run --with` keeps it
out of `pyproject.toml`. Decks are gitignored (`*.pptx`).

All three outputs come from one `compute_awards` in `src/sleeper_analyst/awards.py`,
so the email, the markdown recap, and the deck cannot disagree.

Two of its inputs are **unofficial** Sleeper endpoints: draft picks, and weekly
projections (which live on `api.sleeper.com`, a different host from the documented
v1 API). Each degrades independently — if one is unavailable the awards that depend
on it are skipped with a note, and if the whole award build fails the decision
packet still sends.

Secrets live in `.env` locally (see `.env.example`) and in GitHub Actions
repository secrets for the scheduled run: `ANTHROPIC_API_KEY` plus
`SMTP_PASSWORD` (Gmail app password) or `WEBHOOK_URL` (Slack/Discord).

The weekly workflow commits `data/` back to the repo after each run so the next
week's self-grading sees the prior analysis, and uploads the packet as an artifact.

## Tests

```sh
uv run pytest
```

Tests run entirely offline against recorded fixtures in `tests/fixtures/`
(re-record with `uv run python tests/record_fixtures.py <league_id>`).

## Notes

- The `[byes]` table in config holds the 2026 bye weeks; update it each season.
- The Sleeper API is read-only and unauthenticated; all moves are executed by you in the app.
