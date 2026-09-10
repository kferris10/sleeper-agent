# sleeper-analyst

Weekly Sleeper fantasy football analyst. Every Tuesday it pulls league state from the
Sleeper API, has Claude analyze it, and delivers a decision packet (recap, lineup,
waivers, trades) you execute in the Sleeper app. Full design: `plans/PROJECT_PLAN.md`.

**Status: Phase 3** — full pipeline: `run` chains collect → Claude analysis → email
delivery, scheduled Tuesdays via GitHub Actions (`.github/workflows/weekly.yml`).

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

`run` is idempotent per week: once a week's packet is delivered it exits early
unless you pass `--force`. Any exception sends a short "run FAILED" alert through
the same delivery channel.

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
