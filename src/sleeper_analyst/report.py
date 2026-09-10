"""Render an Analysis into the delivery packet (markdown + email-safe HTML).

The packet is built to be executed in the Sleeper app in under five minutes:
recap, lineup table (changes from the current starters flagged), waiver
checklist in priority order, paste-ready trade pitches, watchlist.
"""

from __future__ import annotations

import html as html_mod
from sleeper_analyst.sleeper.models import Analysis, LineupRec, WeeklyContext


def current_starter_ids(context: WeeklyContext) -> set[str]:
    return {p.id for p in context.my_team.players if p.is_starter}


def is_change(rec: LineupRec, starter_ids: set[str]) -> bool:
    return rec.player_id not in starter_ids


def subject_line(context: WeeklyContext) -> str:
    return f"Week {context.upcoming_week} decision packet — {context.league.name}"


# ---------------------------------------------------------------------------
# Markdown


def render_markdown(analysis: Analysis, context: WeeklyContext) -> str:
    starters = current_starter_ids(context)
    lines: list[str] = [f"# Week {context.upcoming_week} decision packet — {context.league.name}", ""]

    r = analysis.recap
    lines.append(f"## Recap — week {context.completed_week}")
    lines.append("")
    lines.append(r.summary)
    if r.self_grade:
        lines.append(f"\n**Self-grade:** {r.self_grade}")
    if r.what_worked:
        lines.append("\nWhat worked: " + "; ".join(r.what_worked))
    if r.what_hurt:
        lines.append("\nWhat hurt: " + "; ".join(r.what_hurt))
    lines.append("")

    opp = f" vs {context.upcoming_matchup.opponent}" if context.upcoming_matchup else ""
    lines.append(f"## Lineup — week {context.upcoming_week}{opp}")
    lines.append("")
    lines.append("| Slot | Player | Why |")
    lines.append("|---|---|---|")
    for rec in analysis.lineup:
        player = f"**{rec.player}** ← change" if is_change(rec, starters) else rec.player
        lines.append(f"| {rec.slot} | {player} | {rec.reason} |")
    lines.append("")

    if analysis.bench:
        lines.append("## Bench")
        lines.append("")
        for b in analysis.bench:
            lines.append(f"- {b.player} — {b.reason}")
        lines.append("")

    if analysis.waivers:
        lines.append("## Waiver claims — submit in this order")
        lines.append("")
        for w in analysis.waivers:
            drop = f", drop **{w.drop}**" if w.drop else ""
            bid = f" (bid ${w.faab_bid})" if w.faab_bid is not None else ""
            lines.append(f"- [ ] {w.priority}. Add **{w.add}**{drop}{bid} — {w.reason}")
        lines.append("")

    if analysis.trades:
        lines.append("## Trade ideas")
        lines.append("")
        for t in analysis.trades:
            lines.append(f"### To {t.partner}")
            lines.append(f"Give {', '.join(t.give)} — get {', '.join(t.get)}")
            lines.append("")
            lines.append(f"> {t.pitch}")
            lines.append("")
            lines.append(f"- Why it helps me: {t.why_it_helps_me}")
            lines.append(f"- Why they might accept: {t.why_they_might_accept}")
            lines.append("")

    if analysis.watchlist:
        lines.append("## Watchlist")
        lines.append("")
        for wl in analysis.watchlist:
            lines.append(f"- {wl.player} — {wl.note}")
        lines.append("")

    if analysis.confidence_notes:
        lines.append(f"_{analysis.confidence_notes}_")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML (email-safe: inline styles only, single column, no external assets)

_STYLES = {
    "body": "font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#1a1a2e;"
            "max-width:640px;margin:0 auto;padding:16px;line-height:1.45;",
    "h1": "font-size:20px;border-bottom:2px solid #16324f;padding-bottom:6px;",
    "h2": "font-size:16px;color:#16324f;margin:20px 0 8px;",
    "table": "border-collapse:collapse;width:100%;font-size:14px;",
    "th": "text-align:left;padding:6px 8px;border-bottom:2px solid #16324f;",
    "td": "padding:6px 8px;border-bottom:1px solid #d9d9e0;vertical-align:top;",
    "change": "background:#fff3cd;font-weight:bold;",
    "pitch": "border-left:3px solid #16324f;margin:8px 0;padding:6px 12px;"
             "background:#f4f6f8;font-style:italic;",
    "muted": "color:#5a5a6e;font-size:13px;",
}


def render_html(analysis: Analysis, context: WeeklyContext) -> str:
    esc = html_mod.escape
    starters = current_starter_ids(context)
    out: list[str] = [f'<div style="{_STYLES["body"]}">']
    out.append(f'<h1 style="{_STYLES["h1"]}">Week {context.upcoming_week} decision packet '
               f"&mdash; {esc(context.league.name)}</h1>")

    r = analysis.recap
    out.append(f'<h2 style="{_STYLES["h2"]}">Recap &mdash; week {context.completed_week}</h2>')
    out.append(f"<p>{esc(r.summary)}</p>")
    if r.self_grade:
        out.append(f"<p><b>Self-grade:</b> {esc(r.self_grade)}</p>")
    if r.what_worked or r.what_hurt:
        out.append(f'<p style="{_STYLES["muted"]}">')
        if r.what_worked:
            out.append("Worked: " + esc("; ".join(r.what_worked)) + "<br>")
        if r.what_hurt:
            out.append("Hurt: " + esc("; ".join(r.what_hurt)))
        out.append("</p>")

    opp = f" vs {esc(context.upcoming_matchup.opponent)}" if context.upcoming_matchup else ""
    out.append(f'<h2 style="{_STYLES["h2"]}">Lineup &mdash; week {context.upcoming_week}{opp}</h2>')
    out.append(f'<table style="{_STYLES["table"]}"><tr>'
               f'<th style="{_STYLES["th"]}">Slot</th>'
               f'<th style="{_STYLES["th"]}">Player</th>'
               f'<th style="{_STYLES["th"]}">Why</th></tr>')
    for rec in analysis.lineup:
        row_extra = _STYLES["change"] if is_change(rec, starters) else ""
        name = esc(rec.player) + (" &larr; change" if row_extra else "")
        out.append(f'<tr><td style="{_STYLES["td"]}{row_extra}">{esc(rec.slot)}</td>'
                   f'<td style="{_STYLES["td"]}{row_extra}">{name}</td>'
                   f'<td style="{_STYLES["td"]}{row_extra}">{esc(rec.reason)}</td></tr>')
    out.append("</table>")

    if analysis.bench:
        out.append(f'<h2 style="{_STYLES["h2"]}">Bench</h2><ul>')
        for b in analysis.bench:
            out.append(f"<li>{esc(b.player)} &mdash; {esc(b.reason)}</li>")
        out.append("</ul>")

    if analysis.waivers:
        out.append(f'<h2 style="{_STYLES["h2"]}">Waiver claims &mdash; submit in this order</h2><ol>')
        for w in analysis.waivers:
            drop = f", drop <b>{esc(w.drop)}</b>" if w.drop else ""
            bid = f" (bid ${w.faab_bid})" if w.faab_bid is not None else ""
            out.append(f"<li>&#9744; Add <b>{esc(w.add)}</b>{drop}{bid} &mdash; {esc(w.reason)}</li>")
        out.append("</ol>")

    if analysis.trades:
        out.append(f'<h2 style="{_STYLES["h2"]}">Trade ideas</h2>')
        for t in analysis.trades:
            out.append(f"<p><b>To {esc(t.partner)}:</b> give {esc(', '.join(t.give))} &mdash; "
                       f"get {esc(', '.join(t.get))}</p>")
            out.append(f'<div style="{_STYLES["pitch"]}">{esc(t.pitch)}</div>')
            out.append(f'<p style="{_STYLES["muted"]}">Helps me: {esc(t.why_it_helps_me)}<br>'
                       f"They accept because: {esc(t.why_they_might_accept)}</p>")

    if analysis.watchlist:
        out.append(f'<h2 style="{_STYLES["h2"]}">Watchlist</h2><ul>')
        for wl in analysis.watchlist:
            out.append(f"<li>{esc(wl.player)} &mdash; {esc(wl.note)}</li>")
        out.append("</ul>")

    if analysis.confidence_notes:
        out.append(f'<p style="{_STYLES["muted"]}"><i>{esc(analysis.confidence_notes)}</i></p>')

    out.append("</div>")
    return "\n".join(out)
