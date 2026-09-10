"""Call Claude on a WeeklyContext and parse/validate the Analysis.

Reasoning arrives inside <analysis>...</analysis>, the decision packet inside
<result>...</result> as strict JSON. Every player_id in the result must exist
in the context (lineup/drops from my roster, adds from the free-agent list) —
a violation triggers one corrective re-prompt, then a hard failure.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from sleeper_analyst.collect import slot_names
from sleeper_analyst.config import Settings
from sleeper_analyst.sleeper.models import Analysis, WeeklyContext

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
RESULT_RE = re.compile(r"<result>\s*(.*?)\s*</result>", re.DOTALL)
TOKEN_BUDGET = 30_000  # warn/trim threshold for the rendered user prompt

_env = Environment(loader=FileSystemLoader(PROMPTS_DIR), autoescape=False, trim_blocks=False)


class AnalysisError(Exception):
    """The model output could not be parsed or failed validation."""


def estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough chars/4 heuristic; only used for the budget warning


def render_system(settings: Settings, context: WeeklyContext) -> str:
    return _env.get_template("system.md").render(
        philosophy=settings.philosophy,
        slots=slot_names(context.league.roster_positions),
        league_is_faab=context.league.waiver_type == "FAAB",
        trade_deadline_week=context.league.trade_deadline_week,
    )


def trim_free_agents(context: WeeklyContext, keep_fraction: float = 0.5) -> WeeklyContext:
    trimmed = context.model_copy(deep=True)
    trimmed.free_agents = {
        pos: agents[: max(2, int(len(agents) * keep_fraction))]
        for pos, agents in context.free_agents.items()
    }
    return trimmed


def render_user(context: WeeklyContext) -> str:
    template = _env.get_template("user.md.j2")
    text = template.render(ctx=context)
    if estimate_tokens(text) > TOKEN_BUDGET:
        logger.warning(
            "Rendered context ~%d tokens (> %d budget); trimming free-agent list",
            estimate_tokens(text), TOKEN_BUDGET,
        )
        text = template.render(ctx=trim_free_agents(context))
        if estimate_tokens(text) > TOKEN_BUDGET:
            logger.warning("Context still ~%d tokens after trim", estimate_tokens(text))
    return text


def extract_result(text: str) -> dict:
    match = RESULT_RE.search(text)
    if not match:
        raise AnalysisError("no <result>...</result> block found in the response")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"<result> block is not valid JSON: {exc}") from exc


def validate_analysis(data: dict, context: WeeklyContext) -> Analysis:
    try:
        analysis = Analysis.model_validate(data)
    except ValidationError as exc:
        raise AnalysisError(f"result JSON does not match the Analysis schema: {exc}") from exc

    roster_ids = {p.id for p in context.my_team.players}
    fa_ids = {fa.id for agents in context.free_agents.values() for fa in agents}
    errors: list[str] = []

    expected_slots = slot_names(context.league.roster_positions)
    got_slots = [rec.slot for rec in analysis.lineup]
    if sorted(got_slots) != sorted(expected_slots):
        errors.append(
            f"lineup slots must be exactly {expected_slots}, got {got_slots}"
        )
    for rec in analysis.lineup:
        if rec.player_id not in roster_ids:
            errors.append(f"lineup player_id {rec.player_id!r} ({rec.player}) is not on my roster")

    for claim in analysis.waivers:
        if claim.add_player_id not in fa_ids:
            errors.append(
                f"waiver add_player_id {claim.add_player_id!r} ({claim.add}) is not in the free-agent list"
            )
        if claim.drop_player_id is not None and claim.drop_player_id not in roster_ids:
            errors.append(
                f"waiver drop_player_id {claim.drop_player_id!r} ({claim.drop}) is not on my roster"
            )
        if (
            claim.faab_bid is not None
            and context.my_team.faab_remaining is not None
            and claim.faab_bid > context.my_team.faab_remaining
        ):
            errors.append(
                f"FAAB bid {claim.faab_bid} exceeds remaining budget {context.my_team.faab_remaining}"
            )

    if errors:
        raise AnalysisError("; ".join(errors))
    return analysis


def _generate_with_claude(settings: Settings) -> Callable[[str, str], str]:
    import anthropic

    client = anthropic.Anthropic()
    cfg = settings.claude
    tools = []
    if cfg.web_search:
        tools.append(
            {"type": "web_search_20260209", "name": "web_search", "max_uses": cfg.web_search_max_uses}
        )

    def generate(system: str, user: str) -> str:
        # Streaming keeps long responses under HTTP timeouts; server-side
        # fallback re-runs the request on another model on a safety refusal.
        with client.beta.messages.stream(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            tools=tools,
            messages=[{"role": "user", "content": user}],
            # web-search iterations re-read the whole prompt; caching it cuts input cost ~90%
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise AnalysisError(f"model refused the request: {details}")
        if response.stop_reason == "max_tokens":
            logger.warning("Response hit max_tokens=%d; result may be truncated", cfg.max_tokens)
        logger.info(
            "Claude call done: model=%s in=%d out=%d",
            response.model, response.usage.input_tokens, response.usage.output_tokens,
        )
        return "".join(block.text for block in response.content if block.type == "text")

    return generate


def run_analysis(
    context: WeeklyContext,
    settings: Settings,
    generate: Callable[[str, str], str] | None = None,
) -> tuple[Analysis, str]:
    """Analyze one week. Returns (validated Analysis, raw model text).

    `generate(system, user) -> text` is injectable for offline tests; the
    default calls the Claude API.
    """
    generate = generate or _generate_with_claude(settings)
    system = render_system(settings, context)
    user = render_user(context)

    raw = generate(system, user)
    try:
        return validate_analysis(extract_result(raw), context), raw
    except AnalysisError as exc:
        logger.warning("Analysis failed validation (%s); re-prompting once", exc)
        retry_user = (
            f"{user}\n\n---\n\nYour previous attempt failed validation: {exc}\n\n"
            "Produce the packet again. Fix every listed problem, copy player_id values "
            "exactly from the tables above, and follow the output contract "
            "(<analysis>...</analysis> then <result>valid JSON</result>)."
        )
        raw = generate(system, retry_user)
        return validate_analysis(extract_result(raw), context), raw
