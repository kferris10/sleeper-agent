# Sleeper Fantasy Football Weekly Analyst — Project Plan

## Goal

Every Tuesday morning, a scheduled job pulls the league state from the Sleeper API, has Claude analyze it, and delivers a decision packet (recap, lineup, waiver claims, trade ideas) to the team owner, who executes the moves in the Sleeper app. The Sleeper API is read-only, so execution is human-in-the-loop by design.

## Non-goals (v1)

- No automated execution of moves (no browser automation).
- No custom projection model; Claude uses Sleeper data plus web search for current news/projections.
- Single league only.

## Stack

- Python 3.11+, `uv` for env/deps
- `httpx` for HTTP, `pydantic` for models, `anthropic` SDK for Claude
- SQLite for history (one file, no server)
- Scheduling: GitHub Actions cron (primary) or local `cron` (fallback)
- Delivery: email via SMTP (v1); Slack/Discord webhook as optional adapter

## Repo layout

```
sleeper-analyst/
├── README.md
├── PROJECT_PLAN.md            # this file
├── pyproject.toml
├── config.example.toml        # league_id, user_id, philosophy knobs, delivery settings
├── .env.example               # ANTHROPIC_API_KEY, SMTP creds, webhook URLs
├── src/sleeper_analyst/
│   ├── __init__.py
│   ├── cli.py                 # `sleeper-analyst run|collect|analyze|deliver|backfill`
│   ├── config.py              # load config.toml + env
│   ├── sleeper/
│   │   ├── client.py          # thin typed wrapper over api.sleeper.app/v1
│   │   ├── models.py          # pydantic models for league, roster, matchup, transaction, player
│   │   └── players.py         # player map cache (24h TTL on disk)
│   ├── collect.py             # builds WeeklyContext from API + DB
│   ├── analyze.py             # calls Claude, parses structured result
│   ├── prompts/
│   │   ├── system.md          # management philosophy + output contract
│   │   └── user.md.j2         # jinja template that renders WeeklyContext
│   ├── deliver/
│   │   ├── base.py
│   │   ├── email.py
│   │   └── webhook.py
│   ├── report.py              # renders analysis → markdown/HTML
│   └── store.py               # SQLite: weekly contexts, analyses, outcomes
├── tests/
│   ├── fixtures/              # recorded Sleeper JSON responses
│   ├── test_client.py
│   ├── test_collect.py
│   └── test_analyze.py        # parse/validate Claude output against schema
└── .github/workflows/weekly.yml
```

## Sleeper endpoints used

| Purpose | Endpoint |
|---|---|
| Current week/season | `GET /state/nfl` |
| League settings, scoring, roster slots | `GET /league/{league_id}` |
| All rosters | `GET /league/{league_id}/rosters` |
| League users (map owner_id → display name) | `GET /league/{league_id}/users` |
| Last week + this week matchups | `GET /league/{league_id}/matchups/{week}` |
| Transactions for recent weeks | `GET /league/{league_id}/transactions/{week}` |
| Traded picks (if league trades picks) | `GET /league/{league_id}/traded_picks` |
| Player map (cache ≤1/day) | `GET /players/nfl?active=true` |
| Trending adds/drops | `GET /players/nfl/trending/{add|drop}?lookback_hours=48&limit=50` |

Rules: no auth needed; stay well under 1000 req/min; retry with backoff on 429/5xx; cache the player map on disk and refuse to refetch inside 24h.

## Data flow

```
cron (Tue 07:00 local)
  → collect.py   : API + DB → WeeklyContext (JSON, target < 30k tokens)
  → analyze.py   : WeeklyContext + prior analysis → Claude (web search enabled) → Analysis
  → store.py     : persist context + analysis
  → report.py    : Analysis → markdown + HTML
  → deliver/     : send packet
```

## Key models

### WeeklyContext (input to Claude)

- `season`, `completed_week`, `upcoming_week`
- `league`: name, scoring_settings (only non-zero keys), roster_positions, waiver type (FAAB or priority), trade deadline week, playoff weeks
- `my_team`: roster_id, record, points for/against, FAAB remaining, waiver position, players[] (id, name, pos, nfl_team, injury_status, depth_chart_order, is_starter, is_ir, bye_week)
- `last_matchup`: opponent name, my/their points, my starters with slot, my bench, their starters
- `upcoming_matchup`: opponent name, their roster summary (starters by position)
- `other_teams[]`: name, record, FAAB remaining, positional counts, notable strengths/surplus (computed: >N startable at a position), recent adds/drops
- `free_agents`: top ~40 by position, each with trending add/drop counts, injury_status, depth_chart_order. Compute as `active players − union(all roster.players)`, filtered to fantasy positions in the league
- `league_activity`: last 2 weeks of transactions, humanized
- `prior_analysis`: last week's recommendations (so Claude can self-grade)
- `standings`: all teams W-L-PF

### Analysis (output from Claude, strict JSON)

```json
{
  "recap": {"summary": "...", "what_worked": [...], "what_hurt": [...], "self_grade": "..."},
  "lineup": [{"slot": "RB1", "player_id": "...", "player": "...", "reason": "..."}],
  "bench": [{"player": "...", "reason": "..."}],
  "waivers": [{"priority": 1, "add_player_id": "...", "add": "...", "drop_player_id": "...", "drop": "...", "faab_bid": 12, "reason": "..."}],
  "trades": [{"partner": "...", "give": [...], "get": [...], "pitch": "...", "why_it_helps_me": "...", "why_they_might_accept": "..."}],
  "watchlist": [{"player": "...", "note": "..."}],
  "confidence_notes": "..."
}
```

Validate with pydantic; every `player_id` must exist in the context (reject hallucinated players and re-prompt once).

## Claude call design

- Model: latest Sonnet-class model (config knob); switch to Opus/Fable if cost is not a concern
- Tools: `web_search` enabled, with the system prompt instructing Claude to check injury/practice reports and current-week projections for the players actually in play before finalizing lineup and waivers
- `system.md` contains: management philosophy knobs from config (risk tolerance, FAAB aggressiveness, trade appetite, positions to prioritize), the output contract, and hard rules (must fill every roster slot legally; no dropping players on IR into a non-IR slot if IR slot available; respect trade deadline; never bid more FAAB than remaining)
- `user.md.j2` renders WeeklyContext as compact markdown tables rather than raw JSON to save tokens
- Ask for reasoning first inside `<analysis>` then the JSON inside `<result>`; parse only `<result>`
- Temperature default; log full request/response to `store.py` for debugging

## Delivery packet format

Email/Slack message with:
1. One-paragraph recap and self-grade
2. Lineup table for the upcoming week (slot → player, bold changes from current starters)
3. Waiver claims in priority order with FAAB bid and drop — formatted as a checklist the owner ticks off in the app
4. Trade proposals with paste-ready pitch text
5. Watchlist

Keep it scannable; the owner should be able to execute everything in under five minutes.

## Implementation phases

### Phase 1 — Client and collector (get data right first)
- [x] `sleeper/client.py` with typed methods for every endpoint above, retries, and a recorded-fixture test mode
- [x] `sleeper/players.py` disk cache with 24h TTL
- [x] `collect.py` producing a complete `WeeklyContext`; `sleeper-analyst collect --week N` writes `context_week_N.json`
- [x] Free-agent computation and bye-week enrichment (2026 bye table hardcoded in `config.example.toml` `[byes]`)
- [x] Tests against fixtures for roster parsing, free-agent set, and matchup pairing (`matchup_id`) — fixtures recorded from a public 2025 league via `tests/record_fixtures.py`; also added `sleeper-analyst setup --username` to look up user_id/league_id

Done when: running `collect` for the current week produces a context you can read top to bottom and it matches what you see in the app.

### Phase 2 — Analyst
- [x] `prompts/system.md` and `prompts/user.md.j2`
- [x] `analyze.py`: build messages, call Claude with web search, parse and validate `<result>`, retry once on validation failure (model: claude-opus-5, streaming, prompt caching, server-side refusal fallback)
- [x] `store.py` schema: `contexts`, `analyses`, `outcomes` (+ prior week's analysis auto-fed back into the context for self-grading)
- [x] `sleeper-analyst analyze --context context_week_N.json` runs offline from a saved context for fast iteration
- [x] Token budget check: warn if rendered context exceeds ~30k tokens; trim free-agent list first
- Improvement noted for Phase 4: include rival rosters player-by-player so trade pitches can name specific targets

Done when: analysis for a real week is something you'd actually act on, and hallucinated player IDs are impossible.

### Phase 3 — Delivery and scheduling
- [ ] `report.py` markdown + HTML renderers
- [ ] `deliver/email.py` (SMTP) and `deliver/webhook.py` (Slack/Discord)
- [ ] `sleeper-analyst run` chains collect → analyze → store → deliver, idempotent per week (skip if already delivered unless `--force`)
- [ ] `.github/workflows/weekly.yml`: Tuesday 07:00 America/New_York (adjust), secrets for API key and SMTP, uploads packet as artifact too
- [ ] Failure alerting: any exception sends a short "run failed" message through the same delivery channel

### Phase 4 — Feedback loop and polish
- [ ] `outcomes` table: after the following week, record actual points for recommended starters vs benched players and whether waiver claims were won (from transactions endpoint)
- [ ] Include last two weeks of outcomes in the context so Claude calibrates
- [ ] `sleeper-analyst backfill --weeks 1-N` to build history mid-season
- [ ] Config knob review: tune philosophy prompt after 3–4 weeks of packets

### Later / optional
- Approve-and-execute: reply-to-email or Slack buttons that trigger a Playwright job against sleeper.app (review Sleeper's terms first)
- In-week injury watcher (Sat/Sun morning) that re-checks `injury_status` for starters and pings if someone is ruled out
- Multi-league support

## Config knobs (`config.toml`)

```toml
[league]
league_id = ""
user_id = ""            # your Sleeper user_id (not username; usernames change)
timezone = "America/New_York"

[philosophy]
risk_tolerance = "medium"        # low | medium | high
faab_aggressiveness = "medium"   # how much of remaining budget to spend on a top target
trade_appetite = "opportunistic" # none | opportunistic | active
priorities = ["RB depth", "streaming DEF"]
notes = "Free text guidance for Claude, e.g. 'never start rookies week 1'"

[claude]
model = "claude-sonnet-latest"
max_tokens = 8000
web_search = true

[delivery]
channel = "email"                # email | slack | discord
email_to = ""
```

## Open questions for the owner

1. FAAB or waiver priority? Trade deadline week? (Both readable from `league.settings`, but confirm.)
2. Timezone and preferred delivery time.
3. Email vs Slack/Discord for v1.
4. Any house rules Claude should know that aren't encoded in Sleeper settings.
