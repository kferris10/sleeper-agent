"""Render an Analysis into the delivery packet (markdown + email-safe HTML).

The email leads with a pure action list — set this lineup, submit these
claims, send these offers — executable in the Sleeper app in a couple of
minutes, followed by the full breakdown (recap, reasoning, watchlist) for
when the owner wants the why.
"""

from __future__ import annotations

import html as html_mod
from sleeper_analyst.awards import (
    LATE_ROUND_START,
    ZERO_CLUB_THRESHOLD,
    Awards,
    Row as AwardRow,
    Team as AwardTeam,
    drafted_note,
    fmt,
    signed,
)
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


def render_markdown(
    analysis: Analysis, context: WeeklyContext, awards: Awards | None = None
) -> str:
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

    # -- full breakdown below the fold --------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("# Full breakdown")
    lines.append("")

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

    lines.append("## Lineup reasoning")
    lines.append("")
    lines.append("| Slot | Player | Why |")
    lines.append("|---|---|---|")
    for rec in analysis.lineup:
        lines.append(f"| {rec.slot} | {rec.player} | {rec.reason} |")
    lines.append("")

    if analysis.bench:
        lines.append("## Bench")
        lines.append("")
        for b in analysis.bench:
            lines.append(f"- {b.player} — {b.reason}")
        lines.append("")

    if analysis.waivers:
        lines.append("## Waiver reasoning")
        lines.append("")
        for w in analysis.waivers:
            lines.append(f"- {w.priority}. {w.add} — {w.reason}")
        lines.append("")

    if analysis.trades:
        lines.append("## Trade reasoning")
        lines.append("")
        for t in analysis.trades:
            lines.append(f"- **{t.partner}**: helps me — {t.why_it_helps_me} "
                         f"They accept because — {t.why_they_might_accept}")
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

    if awards is not None:
        lines += ["---", "", f"# 🏆 League awards — week {awards.week}", ""]
        lines.append(render_awards_markdown(awards))

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


def render_html(
    analysis: Analysis, context: WeeklyContext, awards: Awards | None = None
) -> str:
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

    # -- full breakdown below the fold --------------------------------------
    out.append('<hr style="margin:24px 0;border:none;border-top:2px solid #16324f;">')
    out.append(f'<h1 style="{_STYLES["h1"]}">Full breakdown</h1>')

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

    out.append(f'<h2 style="{_STYLES["h2"]}">Lineup reasoning</h2>')
    out.append(f'<table style="{_STYLES["table"]}"><tr>'
               f'<th style="{_STYLES["th"]}">Slot</th>'
               f'<th style="{_STYLES["th"]}">Player</th>'
               f'<th style="{_STYLES["th"]}">Why</th></tr>')
    for rec in analysis.lineup:
        out.append(f'<tr><td style="{_STYLES["td"]}">{esc(rec.slot)}</td>'
                   f'<td style="{_STYLES["td"]}">{esc(rec.player)}</td>'
                   f'<td style="{_STYLES["td"]}">{esc(rec.reason)}</td></tr>')
    out.append("</table>")

    if analysis.bench:
        out.append(f'<h2 style="{_STYLES["h2"]}">Bench</h2><ul>')
        for b in analysis.bench:
            out.append(f"<li>{esc(b.player)} &mdash; {esc(b.reason)}</li>")
        out.append("</ul>")

    if analysis.waivers:
        out.append(f'<h2 style="{_STYLES["h2"]}">Waiver reasoning</h2><ol>')
        for w in analysis.waivers:
            out.append(f"<li>{esc(w.add)} &mdash; {esc(w.reason)}</li>")
        out.append("</ol>")

    if analysis.trades:
        out.append(f'<h2 style="{_STYLES["h2"]}">Trade reasoning</h2>')
        for t in analysis.trades:
            out.append(f'<p style="{_STYLES["muted"]}"><b>{esc(t.partner)}</b>: '
                       f"helps me &mdash; {esc(t.why_it_helps_me)}<br>"
                       f"They accept because &mdash; {esc(t.why_they_might_accept)}</p>")

    if analysis.watchlist:
        out.append(f'<h2 style="{_STYLES["h2"]}">Watchlist</h2><ul>')
        for wl in analysis.watchlist:
            out.append(f"<li>{esc(wl.player)} &mdash; {esc(wl.note)}</li>")
        out.append("</ul>")

    if analysis.confidence_notes:
        out.append(f'<p style="{_STYLES["muted"]}"><i>{esc(analysis.confidence_notes)}</i></p>')

    if awards is not None:
        out.append('<hr style="margin:24px 0;border:none;border-top:2px solid #16324f;">')
        out.append(
            f'<h1 style="{_STYLES["h1"]}">&#127942; League awards '
            f"&mdash; week {awards.week}</h1>"
        )
        out.append(render_awards_html(awards))

    out.append("</div>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# League awards
#
# The fun half of the packet: all 15 categories for the whole league, appended
# below the decision packet so the actionable part still leads. Both renderers
# take an Awards (see awards.py) and emit a section, not a whole document --
# the caller supplies its own top-level heading.


def _award_scoreline(t: AwardTeam) -> str:
    """Winner first, so it reads like a box score."""
    if t.won:
        return (
            f"{t.name} {fmt(t.points)} — {fmt(t.opponent_points)} {t.opponent} "
            f"(margin: {fmt(abs(t.margin))})"
        )
    return (
        f"{t.opponent} {fmt(t.opponent_points)} — {fmt(t.points)} {t.name} "
        f"(margin: {fmt(abs(t.margin))})"
    )


def _who(row: AwardRow) -> str:
    return f"**{row.name}** ({row.pos}, {row.nfl_team})"


def render_awards_markdown(a: Awards) -> str:
    out: list[str] = []
    add = out.append

    add(
        f"{len(a.teams)} teams, {len(a.teams) // 2} games, and a *lot* of decisions "
        f"that will not be repeated. League average: **{fmt(a.league_average)}** points."
    )
    add("")

    champ, chump = a.champ, a.chump

    add("## 🏆 Team of the Week")
    add("")
    add(
        f"**{champ.name}** ({champ.manager}) — **{fmt(champ.points)}**, "
        f"beating {champ.opponent} by {fmt(abs(champ.margin))}."
    )
    add("")
    add("Carried by:")
    for r in a.champ_top:
        add(f"- {_who(r)} — **{fmt(r.points)}** ({r.slot})")
    add("")

    add("## 💀 Team of the Weak")
    add("")
    add(
        f"**{chump.name}** ({chump.manager}) — **{fmt(chump.points)}**, "
        f"{fmt(champ.points - chump.points)} behind the league leader."
    )
    add("")
    add("The lowlights:")
    for r in a.chump_duds:
        add(f"- {_who(r)} — {fmt(r.points)} ({r.slot})")
    add("")
    add(
        "In fairness, "
        + " and ".join(f"{_who(r)} ({fmt(r.points)})" for r in a.chump_heroes)
        + " showed up. Everyone else filed a police report."
    )
    add("")

    if a.have_projections:
        add("## 📈 Biggest Overachiever")
        add("")
        for r in a.overachievers:
            add(
                f"- {_who(r)} — projected {fmt(r.projected or 0)}, scored "
                f"**{fmt(r.points)}** (**{signed(r.vs_proj or 0)}**). Started by {r.team}."
            )
        add("")
        if a.benched_overachiever:
            r = a.benched_overachiever
            add(
                f"And the one nobody got to enjoy: {_who(r)} beat his projection by "
                f"**{signed(r.vs_proj or 0)}** — from {r.team}'s bench."
            )
            add("")

        add("## 📉 Biggest Bust")
        add("")
        for r in a.busts:
            add(
                f"- {_who(r)} — projected {fmt(r.projected or 0)}, scored "
                f"**{fmt(r.points)}** (**{signed(r.vs_proj or 0)}**). Started by {r.team}."
            )
        add("")

    if a.have_draft:
        add(f"## 💎 Late-Round Steal (rounds {LATE_ROUND_START}+)")
        add("")
        for r in a.late_steals:
            seat = f"in {r.team}'s starting lineup" if r.started else f"on {r.team}'s bench"
            add(
                f"- {_who(r)} — **{fmt(r.points)}**, taken {drafted_note(r)}, "
                f"spent Sunday {seat}."
            )
        add("")

        add("## 🎯 Late-Round Steal Somebody Actually Started")
        add("")
        if a.late_steals_started:
            for r in a.late_steals_started:
                add(
                    f"- {_who(r)} — **{fmt(r.points)}** from {drafted_note(r)}. "
                    f"Credit to {r.team} for having the nerve."
                )
        else:
            add("Nobody. Not one manager trusted a late-round pick. Cowards.")
        add("")

    add("## 🔥 The Bench Burner")
    add("")
    for b in a.blunders:
        add(
            f"- **{b.team.name}** started {_who(b.started)} for {fmt(b.started.points)} "
            f"while {_who(b.benched)} put up **{fmt(b.benched.points)}** on the bench "
            f"(−{fmt(b.gap)})."
        )
    if a.blunders:
        top = a.blunders[0]
        if top.gap > abs(top.team.margin) and not top.team.won:
            add("")
            add(
                f"  ↳ Worth noting: {top.team.name} lost by {fmt(abs(top.team.margin))}. "
                f"That one decision was the whole game."
            )
    add("")

    add("## 🧠 Manager Efficiency")
    add("")
    best, worst = a.efficiency[0], a.efficiency[-1]
    add(
        f"🥇 **Sharpest:** {best.team.name} — {fmt(best.team.points)} of a possible "
        f"{fmt(best.optimal)} (**{best.pct:.0f}%**)."
    )
    add(
        f"🤕 **Roughest:** {worst.team.name} — {fmt(worst.team.points)} of a possible "
        f"{fmt(worst.optimal)} (**{worst.pct:.0f}%**). There was a much better team "
        f"on that roster."
    )
    add("")
    add("| Team | Actual | Optimal | % |")
    add("| --- | ---: | ---: | ---: |")
    for e in a.efficiency:
        add(f"| {e.team.name} | {fmt(e.team.points)} | {fmt(e.optimal)} | {e.pct:.0f}% |")
    add("")

    add("## 😤 Luckiest & Unluckiest")
    add("")
    if a.unlucky:
        beaten = sum(1 for t in a.teams if t.points < a.unlucky.points)
        add(
            f"**Unluckiest:** {a.unlucky.name} scored **{fmt(a.unlucky.points)}** — more "
            f"than {beaten} other teams — and still lost to {a.unlucky.opponent} "
            f"({fmt(a.unlucky.opponent_points)})."
        )
    if a.lucky:
        add("")
        add(
            f"**Luckiest:** {a.lucky.name} won with just **{fmt(a.lucky.points)}**, "
            f"drawing {a.lucky.opponent} ({fmt(a.lucky.opponent_points)}). "
            f"A win is a win. Barely."
        )
    add("")

    add("## ⚔️ Closest Game & Biggest Blowout")
    add("")
    add(f"**Nail-biter:** {_award_scoreline(a.closest)}.")
    add("")
    add(f"**Beatdown:** {_award_scoreline(a.blowout)}. Mercy rule, please.")
    add("")

    add(f"## 🥚 The Zero Club (started, scored ≤ {fmt(ZERO_CLUB_THRESHOLD)})")
    add("")
    if a.zero_club:
        for r in a.zero_club:
            add(f"- {r.name} ({r.pos}) — {fmt(r.points)} — started by {r.team}")
    else:
        add("Empty. Somehow every single starter in this league scored. Enjoy it.")
    add("")

    if a.have_draft:
        add("## 🦄 Free Money (undrafted, and started anyway)")
        add("")
        if a.undrafted:
            for r in a.undrafted:
                add(
                    f"- {_who(r)} — **{fmt(r.points)}** from a player nobody spent a pick "
                    f"on. {r.team} just picked him up off the floor."
                )
        else:
            add("Every starter in the league was drafted. A very boring, very safe week.")
        add("")

    if a.have_projections and a.vs_projection:
        add("## 📊 The League vs. The Projections")
        add("")
        total = len(a.vs_projection)
        add(
            f"**{a.beat_projection} of {total}** teams beat their projected total. "
            f"The other {total - a.beat_projection} of us are still calling it a process."
        )
        add("")
        add("| Team | Projected | Actual | Diff |")
        add("| --- | ---: | ---: | ---: |")
        for t in a.vs_projection:
            add(
                f"| {t.name} | {fmt(t.projected or 0)} | {fmt(t.points)} | "
                f"{signed(t.points - (t.projected or 0))} |"
            )
        add("")

    add("## 🤡 Positional High Scores (started only)")
    add("")
    add("| Slot | Player | Points | Team |")
    add("| --- | --- | ---: | --- |")
    for pos, r in a.positional_best:
        add(f"| {pos} | {r.name} | **{fmt(r.points)}** | {r.team} |")
    add("")

    add(f"## 🔮 Week {a.week + 1} Bulletin Board")
    add("")
    add(f"- {chump.name} has nowhere to go but up. Statistically. Probably.")
    add(f"- {champ.name} peaked in week {a.week} and we all know it.")
    if a.blunders:
        add(
            f"- {a.blunders[0].team.name} is now the league's designated "
            f"'check your lineup' reminder."
        )
    add("")

    add("## 📣 If you only read one thing")
    add("")
    add(f"1. **{champ.name}** dropped {fmt(champ.points)} and looked unbeatable.")
    add(f"2. **{chump.name}** managed {fmt(chump.points)} and looked unrecognizable.")
    if a.blunders:
        top = a.blunders[0]
        add(
            f"3. **{top.team.name}** left {fmt(top.gap)} points on the bench in a single "
            f"slot — the week's most expensive click."
        )
    if a.unlucky:
        add(
            f"4. Spare a thought for **{a.unlucky.name}**, who scored "
            f"{fmt(a.unlucky.points)} and lost anyway."
        )
    add("")

    for note in _award_gaps(a):
        add(f"_Note: {note}._")
        add("")

    return "\n".join(out)


def _award_gaps(a: Awards) -> list[str]:
    """Which award families were skipped, if an unofficial endpoint was down."""
    notes = []
    if not a.have_projections:
        notes.append("projections were unavailable, so projection-based awards are skipped")
    if not a.have_draft:
        notes.append("draft data was unavailable, so draft-round awards are skipped")
    return notes


# -- HTML -------------------------------------------------------------------


def _awards_table(esc, headers: list[str], rows: list[list[str]]) -> str:
    cells = "".join(f'<th style="{_STYLES["th"]}">{esc(h)}</th>' for h in headers)
    body = []
    for row in rows:
        tds = "".join(f'<td style="{_STYLES["td"]}">{esc(v)}</td>' for v in row)
        body.append(f"<tr>{tds}</tr>")
    return (
        f'<table style="{_STYLES["table"]}"><tr>{cells}</tr>' + "".join(body) + "</table>"
    )


def render_awards_html(a: Awards) -> str:
    """Email-safe HTML for the same 15 categories, inline styles only."""
    esc = html_mod.escape
    out: list[str] = []
    add = out.append

    def h2(title: str) -> None:
        add(f'<h2 style="{_STYLES["h2"]}">{title}</h2>')

    def who(r: AwardRow) -> str:
        return f"<b>{esc(r.name)}</b> ({esc(r.pos)}, {esc(r.nfl_team)})"

    champ, chump = a.champ, a.chump

    add(
        f'<p style="{_STYLES["muted"]}">{len(a.teams)} teams, {len(a.teams) // 2} games. '
        f"League average: <b>{fmt(a.league_average)}</b> points.</p>"
    )

    h2("&#127942; Team of the Week")
    add(
        f"<p><b>{esc(champ.name)}</b> ({esc(champ.manager)}) &mdash; "
        f"<b>{fmt(champ.points)}</b>, beating {esc(champ.opponent)} by "
        f"{fmt(abs(champ.margin))}.</p><ul>"
    )
    for r in a.champ_top:
        add(f"<li>{who(r)} &mdash; <b>{fmt(r.points)}</b> ({esc(r.slot or '')})</li>")
    add("</ul>")

    h2("&#128128; Team of the Weak")
    add(
        f"<p><b>{esc(chump.name)}</b> ({esc(chump.manager)}) &mdash; "
        f"<b>{fmt(chump.points)}</b>, {fmt(champ.points - chump.points)} behind the "
        f"league leader.</p><ul>"
    )
    for r in a.chump_duds:
        add(f"<li>{who(r)} &mdash; {fmt(r.points)} ({esc(r.slot or '')})</li>")
    add("</ul>")
    add(
        f'<p style="{_STYLES["muted"]}">In fairness, '
        + " and ".join(f"{esc(r.name)} ({fmt(r.points)})" for r in a.chump_heroes)
        + " showed up. Everyone else filed a police report.</p>"
    )

    if a.have_projections:
        h2("&#128200; Biggest Overachiever")
        add("<ul>")
        for r in a.overachievers:
            add(
                f"<li>{who(r)} &mdash; projected {fmt(r.projected or 0)}, scored "
                f"<b>{fmt(r.points)}</b> (<b>{signed(r.vs_proj or 0)}</b>), "
                f"{esc(r.team)}</li>"
            )
        add("</ul>")
        if a.benched_overachiever:
            r = a.benched_overachiever
            add(
                f'<p style="{_STYLES["muted"]}">And the one nobody got to enjoy: '
                f"{who(r)} beat his projection by <b>{signed(r.vs_proj or 0)}</b> "
                f"&mdash; from {esc(r.team)}&#39;s bench.</p>"
            )

        h2("&#128201; Biggest Bust")
        add("<ul>")
        for r in a.busts:
            add(
                f"<li>{who(r)} &mdash; projected {fmt(r.projected or 0)}, scored "
                f"<b>{fmt(r.points)}</b> (<b>{signed(r.vs_proj or 0)}</b>), "
                f"{esc(r.team)}</li>"
            )
        add("</ul>")

    if a.have_draft:
        h2(f"&#128142; Late-Round Steal (rounds {LATE_ROUND_START}+)")
        add("<ul>")
        for r in a.late_steals:
            seat = "started" if r.started else "benched"
            add(
                f"<li>{who(r)} &mdash; <b>{fmt(r.points)}</b>, {esc(drafted_note(r))}, "
                f"{seat} by {esc(r.team)}</li>"
            )
        add("</ul>")

        h2("&#127919; Late-Round Steal Somebody Actually Started")
        if a.late_steals_started:
            add("<ul>")
            for r in a.late_steals_started:
                add(
                    f"<li>{who(r)} &mdash; <b>{fmt(r.points)}</b> from "
                    f"{esc(drafted_note(r))}. Credit to {esc(r.team)}.</li>"
                )
            add("</ul>")
        else:
            add("<p>Nobody. Not one manager trusted a late-round pick. Cowards.</p>")

    h2("&#128293; The Bench Burner")
    add("<ul>")
    for b in a.blunders:
        add(
            f"<li><b>{esc(b.team.name)}</b> started {who(b.started)} for "
            f"{fmt(b.started.points)} while {who(b.benched)} put up "
            f"<b>{fmt(b.benched.points)}</b> on the bench (&minus;{fmt(b.gap)})</li>"
        )
    add("</ul>")
    if a.blunders:
        top = a.blunders[0]
        if top.gap > abs(top.team.margin) and not top.team.won:
            add(
                f'<p style="{_STYLES["muted"]}">{esc(top.team.name)} lost by '
                f"{fmt(abs(top.team.margin))}. That one decision was the whole game.</p>"
            )

    h2("&#129504; Manager Efficiency")
    best, worst = a.efficiency[0], a.efficiency[-1]
    add(
        f"<p><b>Sharpest:</b> {esc(best.team.name)} &mdash; {fmt(best.team.points)} of a "
        f"possible {fmt(best.optimal)} (<b>{best.pct:.0f}%</b>).<br>"
        f"<b>Roughest:</b> {esc(worst.team.name)} &mdash; {fmt(worst.team.points)} of a "
        f"possible {fmt(worst.optimal)} (<b>{worst.pct:.0f}%</b>).</p>"
    )
    add(
        _awards_table(
            esc,
            ["Team", "Actual", "Optimal", "%"],
            [
                [e.team.name, fmt(e.team.points), fmt(e.optimal), f"{e.pct:.0f}%"]
                for e in a.efficiency
            ],
        )
    )

    h2("&#128548; Luckiest &amp; Unluckiest")
    if a.unlucky:
        beaten = sum(1 for t in a.teams if t.points < a.unlucky.points)
        add(
            f"<p><b>Unluckiest:</b> {esc(a.unlucky.name)} scored "
            f"<b>{fmt(a.unlucky.points)}</b> &mdash; more than {beaten} other teams "
            f"&mdash; and still lost to {esc(a.unlucky.opponent)} "
            f"({fmt(a.unlucky.opponent_points)}).</p>"
        )
    if a.lucky:
        add(
            f"<p><b>Luckiest:</b> {esc(a.lucky.name)} won with just "
            f"<b>{fmt(a.lucky.points)}</b>, drawing {esc(a.lucky.opponent)} "
            f"({fmt(a.lucky.opponent_points)}). A win is a win. Barely.</p>"
        )

    h2("&#9876; Closest Game &amp; Biggest Blowout")
    add(
        f"<p><b>Nail-biter:</b> {esc(_award_scoreline(a.closest))}.<br>"
        f"<b>Beatdown:</b> {esc(_award_scoreline(a.blowout))}. Mercy rule, please.</p>"
    )

    h2(f"&#129370; The Zero Club (started, scored &le; {fmt(ZERO_CLUB_THRESHOLD)})")
    if a.zero_club:
        add(
            _awards_table(
                esc,
                ["Player", "Pos", "Points", "Started by"],
                [[r.name, r.pos, fmt(r.points), r.team] for r in a.zero_club],
            )
        )
    else:
        add("<p>Empty. Somehow every single starter in this league scored.</p>")

    if a.have_draft:
        h2("&#129412; Free Money (undrafted, and started anyway)")
        if a.undrafted:
            add("<ul>")
            for r in a.undrafted:
                add(
                    f"<li>{who(r)} &mdash; <b>{fmt(r.points)}</b>, picked up off the "
                    f"floor by {esc(r.team)}</li>"
                )
            add("</ul>")
        else:
            add("<p>Every starter in the league was drafted.</p>")

    if a.have_projections and a.vs_projection:
        h2("&#128202; The League vs. The Projections")
        total = len(a.vs_projection)
        add(
            f"<p><b>{a.beat_projection} of {total}</b> teams beat their projected total. "
            f"The other {total - a.beat_projection} of us are still calling it "
            f"a process.</p>"
        )
        add(
            _awards_table(
                esc,
                ["Team", "Projected", "Actual", "Diff"],
                [
                    [
                        t.name, fmt(t.projected or 0), fmt(t.points),
                        signed(t.points - (t.projected or 0)),
                    ]
                    for t in a.vs_projection
                ],
            )
        )

    h2("&#129313; Positional High Scores (started only)")
    add(
        _awards_table(
            esc,
            ["Slot", "Player", "Points", "Team"],
            [[pos, r.name, fmt(r.points), r.team] for pos, r in a.positional_best],
        )
    )

    h2(f"&#128302; Week {a.week + 1} Bulletin Board")
    add("<ul>")
    add(f"<li>{esc(chump.name)} has nowhere to go but up. Statistically. Probably.</li>")
    add(f"<li>{esc(champ.name)} peaked in week {a.week} and we all know it.</li>")
    if a.blunders:
        add(
            f"<li>{esc(a.blunders[0].team.name)} is now the league&#39;s designated "
            f"&#39;check your lineup&#39; reminder.</li>"
        )
    add("</ul>")

    h2("&#128227; If you only read one thing")
    add("<ol>")
    add(
        f"<li><b>{esc(champ.name)}</b> dropped {fmt(champ.points)} and looked "
        f"unbeatable.</li>"
    )
    add(
        f"<li><b>{esc(chump.name)}</b> managed {fmt(chump.points)} and looked "
        f"unrecognizable.</li>"
    )
    if a.blunders:
        top = a.blunders[0]
        add(
            f"<li><b>{esc(top.team.name)}</b> left {fmt(top.gap)} points on the bench "
            f"in a single slot &mdash; the week&#39;s most expensive click.</li>"
        )
    if a.unlucky:
        add(
            f"<li>Spare a thought for <b>{esc(a.unlucky.name)}</b>, who scored "
            f"{fmt(a.unlucky.points)} and lost anyway.</li>"
        )
    add("</ol>")

    for note in _award_gaps(a):
        add(f'<p style="{_STYLES["muted"]}"><i>Note: {esc(note)}.</i></p>')

    return "\n".join(out)
