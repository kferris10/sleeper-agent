"""Render an Analysis into the delivery packet (markdown + email-safe HTML).

The email is a pure action list — set this lineup, submit these claims, send
these offers — executable in the Sleeper app in a couple of minutes. All the
reasoning stays in data/analysis_week_N.json (and the DB) for reference.
"""

from __future__ import annotations

import html as html_mod
from sleeper_analyst.sleeper.models import Analysis, LineupRec, WeeklyContext


def current_starter_ids(context: WeeklyContext) -> set[str]:
    return {p.id for p in context.my_team.players if p.is_starter}


def is_change(rec: LineupRec, starter_ids: set[str]) -> bool:
    return rec.player_id not in starter_ids


def subject_line(context: WeeklyContext, tag: str = "") -> str:
    suffix = " — Friday injury update" if tag == "friday" else ""
    return f"Week {context.upcoming_week} moves — {context.league.name}{suffix}"


def lineup_changes(analysis: Analysis, context: WeeklyContext) -> list[LineupRec]:
    starters = current_starter_ids(context)
    return [rec for rec in analysis.lineup if is_change(rec, starters)]


# ---------------------------------------------------------------------------
# Markdown


def render_markdown(analysis: Analysis, context: WeeklyContext) -> str:
    starters = current_starter_ids(context)
    changes = lineup_changes(analysis, context)
    lines: list[str] = [f"# Week {context.upcoming_week} — do these in the Sleeper app", ""]

    step = 1
    lines.append(f"## {step}. Set this lineup")
    lines.append("")
    if changes:
        lines.append("Changes: " + "; ".join(f"{c.slot} → **{c.player}**" for c in changes))
    else:
        lines.append("No changes — current starters are correct.")
    lines.append("")
    lines.append("| Slot | Player |")
    lines.append("|---|---|")
    for rec in analysis.lineup:
        player = f"**{rec.player}** ← change" if is_change(rec, starters) else rec.player
        lines.append(f"| {rec.slot} | {player} |")
    lines.append("")

    if analysis.waivers:
        step += 1
        lines.append(f"## {step}. Submit these waiver claims, in this order")
        lines.append("")
        for w in analysis.waivers:
            drop = f", drop **{w.drop}**" if w.drop else ""
            bid = f", bid ${w.faab_bid}" if w.faab_bid is not None else ""
            lines.append(f"- [ ] {w.priority}. Add **{w.add}**{drop}{bid}")
        lines.append("")

    if analysis.trades:
        step += 1
        lines.append(f"## {step}. Send these trade offers")
        lines.append("")
        for t in analysis.trades:
            lines.append(
                f"- [ ] To **{t.partner}**: offer {', '.join(t.give)} for {', '.join(t.get)}."
                " Message to paste:"
            )
            lines.append("")
            lines.append(f"> {t.pitch}")
            lines.append("")

    lines.append(f"_Full reasoning: data/analysis_week_{context.upcoming_week}.json in the repo._")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML (email-safe: inline styles only, single column, no external assets)

_STYLES = {
    "body": "font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1a1a2e;"
            "max-width:560px;margin:0 auto;padding:16px;line-height:1.45;",
    "h1": "font-size:19px;border-bottom:2px solid #16324f;padding-bottom:6px;",
    "h2": "font-size:16px;color:#16324f;margin:20px 0 8px;",
    "table": "border-collapse:collapse;width:100%;font-size:14px;",
    "th": "text-align:left;padding:6px 8px;border-bottom:2px solid #16324f;",
    "td": "padding:6px 8px;border-bottom:1px solid #d9d9e0;vertical-align:top;",
    "change": "background:#fff3cd;font-weight:bold;",
    "pitch": "border-left:3px solid #16324f;margin:8px 0 16px;padding:6px 12px;"
             "background:#f4f6f8;font-style:italic;",
    "muted": "color:#5a5a6e;font-size:12px;",
}


def render_html(analysis: Analysis, context: WeeklyContext) -> str:
    esc = html_mod.escape
    starters = current_starter_ids(context)
    changes = lineup_changes(analysis, context)
    out: list[str] = [f'<div style="{_STYLES["body"]}">']
    out.append(f'<h1 style="{_STYLES["h1"]}">Week {context.upcoming_week} &mdash; '
               "do these in the Sleeper app</h1>")

    step = 1
    out.append(f'<h2 style="{_STYLES["h2"]}">{step}. Set this lineup</h2>')
    if changes:
        out.append("<p>Changes: "
                   + "; ".join(f"{esc(c.slot)} &rarr; <b>{esc(c.player)}</b>" for c in changes)
                   + "</p>")
    else:
        out.append("<p>No changes &mdash; current starters are correct.</p>")
    out.append(f'<table style="{_STYLES["table"]}"><tr>'
               f'<th style="{_STYLES["th"]}">Slot</th>'
               f'<th style="{_STYLES["th"]}">Player</th></tr>')
    for rec in analysis.lineup:
        row_extra = _STYLES["change"] if is_change(rec, starters) else ""
        name = esc(rec.player) + (" &larr; change" if row_extra else "")
        out.append(f'<tr><td style="{_STYLES["td"]}{row_extra}">{esc(rec.slot)}</td>'
                   f'<td style="{_STYLES["td"]}{row_extra}">{name}</td></tr>')
    out.append("</table>")

    if analysis.waivers:
        step += 1
        out.append(f'<h2 style="{_STYLES["h2"]}">{step}. Submit these waiver claims, '
                   "in this order</h2><ol>")
        for w in analysis.waivers:
            drop = f", drop <b>{esc(w.drop)}</b>" if w.drop else ""
            bid = f", bid ${w.faab_bid}" if w.faab_bid is not None else ""
            out.append(f"<li>&#9744; Add <b>{esc(w.add)}</b>{drop}{bid}</li>")
        out.append("</ol>")

    if analysis.trades:
        step += 1
        out.append(f'<h2 style="{_STYLES["h2"]}">{step}. Send these trade offers</h2>')
        for t in analysis.trades:
            out.append(f"<p>&#9744; To <b>{esc(t.partner)}</b>: offer {esc(', '.join(t.give))} "
                       f"for {esc(', '.join(t.get))}. Message to paste:</p>")
            out.append(f'<div style="{_STYLES["pitch"]}">{esc(t.pitch)}</div>')

    out.append(f'<p style="{_STYLES["muted"]}">Full reasoning: '
               f"data/analysis_week_{context.upcoming_week}.json in the repo.</p>")
    out.append("</div>")
    return "\n".join(out)
