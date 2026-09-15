"""Render the weekly league awards as a PowerPoint deck.

Consumes the same league_awards.compute_awards output as week1_recap.py, so the
deck and the markdown post always agree.

python-pptx is not a project dependency -- it is only needed for this one script,
so run it ephemerally rather than adding it to pyproject.toml:

    uv run --with python-pptx python scripts/week1_deck.py --week 1
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Inches, Pt

from sleeper_analyst.awards import (
    Awards,
    LATE_ROUND_START,
    Row,
    ZERO_CLUB_THRESHOLD,
    drafted_note,
    fmt,
    load_awards,
    signed,
)

from sleeper_analyst.config import load_settings

logger = logging.getLogger(__name__)

# -- theme ------------------------------------------------------------------
# A dark broadcast look: near-black ground, one warm accent, and a lot of empty
# space so the numbers carry the slide. Deliberately not an Office template.

INK = RGBColor(0x0E, 0x11, 0x16)  # slide background
PANEL = RGBColor(0x17, 0x1C, 0x26)  # table fills, stat block
RULE = RGBColor(0x26, 0x2D, 0x3B)  # hairlines
GOLD = RGBColor(0xF5, 0xB9, 0x42)  # accent / headline numbers
GREEN = RGBColor(0x5B, 0xD9, 0x8A)  # good
RED = RGBColor(0xFF, 0x6B, 0x6B)  # bad
WHITE = RGBColor(0xF7, 0xF9, 0xFC)
MUTED = RGBColor(0x8D, 0x97, 0xA8)

FONT = "Segoe UI"

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN = Inches(0.9)
BODY_W = Inches(7.6)  # leaves room for the stat block on the right
FULL_W = SLIDE_W - MARGIN * 2


# -- primitives -------------------------------------------------------------


def new_slide(prs: Presentation):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = INK
    return slide


def text(
    slide,
    left,
    top,
    width,
    height,
    body: str,
    *,
    size: int = 18,
    color: RGBColor = WHITE,
    bold: bool = False,
    align=PP_ALIGN.LEFT,
    spacing: float = 1.0,
    anchor=MSO_ANCHOR.TOP,
):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, line in enumerate(body.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(6)
        font = p.font
        font.name = FONT
        font.size = Pt(size)
        font.bold = bold
        font.color.rgb = color
    return box


def rect(slide, left, top, width, height, color: RGBColor):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def header(slide, kicker: str, headline: str, subhead: str = "") -> Emu:
    """Accent bar + eyebrow + headline. Returns the y where body content starts."""
    rect(slide, MARGIN, Inches(0.72), Inches(0.62), Inches(0.085), GOLD)
    text(
        slide, MARGIN, Inches(0.95), FULL_W, Inches(0.35),
        kicker.upper(), size=14, color=GOLD, bold=True,
    )
    text(
        slide, MARGIN, Inches(1.38), FULL_W, Inches(1.0),
        headline, size=38, color=WHITE, bold=True, spacing=0.95,
    )
    y = Inches(2.35)
    if subhead:
        text(slide, MARGIN, y, BODY_W, Inches(0.6), subhead, size=17, color=MUTED)
        y = Inches(2.95)
    return y


def stat_block(slide, value: str, label: str, color: RGBColor = GOLD):
    """The oversized number parked on the right-hand third."""
    left, top, width = Inches(9.15), Inches(1.5), Inches(3.3)
    rect(slide, left, top, width, Inches(2.5), PANEL)
    text(
        slide, left, top + Inches(0.42), width, Inches(1.5),
        value, size=64, color=color, bold=True, align=PP_ALIGN.CENTER,
    )
    text(
        slide, left, top + Inches(1.85), width, Inches(0.45),
        label.upper(), size=12, color=MUTED, bold=True, align=PP_ALIGN.CENTER,
    )


def bullets(slide, top: Emu, lines: list[str], *, size: int = 17, width=BODY_W):
    """Dot-led lines."""
    y = top
    for line in lines:
        rect(slide, MARGIN, y + Inches(0.16), Inches(0.09), Inches(0.09), GOLD)
        text(
            slide, MARGIN + Inches(0.28), y, width - Inches(0.28), Inches(0.5),
            line, size=size, color=WHITE,
        )
        y += Inches(0.62)
    return y


def runners_up(slide, top: Emu, lines: list[str], *, label: str = "Also", size: int = 15):
    """Items 2..n of a ranking. The winner is already the headline, so listing it
    again in the first bullet just reads as a duplicate."""
    if not lines:
        return top
    text(slide, MARGIN, top, BODY_W, Inches(0.35), label.upper(), size=13,
         color=MUTED, bold=True)
    return bullets(slide, top + Inches(0.45), lines, size=size)


def panel(
    slide,
    left,
    *,
    label: str,
    label_color: RGBColor,
    title: str,
    stat: str,
    lines: list[str],
    top=Inches(2.25),
    width=Inches(5.5),
    height=Inches(4.05),
):
    """One half of a two-up comparison slide: eyebrow, name, big number, detail."""
    rect(slide, left, top, width, height, PANEL)
    pad = Inches(0.4)
    inner = width - pad * 2
    text(
        slide, left + pad, top + Inches(0.35), inner, Inches(0.3),
        label.upper(), size=12, color=label_color, bold=True,
    )
    text(
        slide, left + pad, top + Inches(0.75), inner, Inches(0.55),
        title, size=23, color=WHITE, bold=True,
    )
    text(
        slide, left + pad, top + Inches(1.4), inner, Inches(0.9),
        stat, size=46, color=label_color, bold=True,
    )
    y = top + Inches(2.2)
    for line in lines:
        text(slide, left + pad, y, inner, Inches(0.38), line, size=13, color=MUTED)
        y += Inches(0.38)


def clear_borders(cell):
    """Drop the table style's gridlines; the row fills do the separating.

    python-pptx has no border API, so the line elements go in by hand.
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        name = tag.split(":")[1]
        for existing in tc_pr.findall(qn(tag)):
            tc_pr.remove(existing)
        ln = OxmlElement(tag)
        ln.set("w", "0")
        ln.append(OxmlElement("a:noFill"))
        # order matters in the schema: lnL, lnR, lnT, lnB come first in tcPr
        tc_pr.insert(
            ["lnL", "lnR", "lnT", "lnB"].index(name), ln
        )


def table(
    slide,
    headers: list[str],
    rows: list[list[str]],
    *,
    top,
    widths: list[float],
    highlight: dict[int, RGBColor] | None = None,
    col_colors: dict[int, RGBColor] | None = None,
    left_cols: set[int] | None = None,
    size: int = 12,
    bottom=Inches(6.55),
):
    """A flat, hand-styled table -- python-pptx's default style is Office blue.

    Row height is derived from the space left above the footer so a 12-team
    table cannot run off the bottom of the slide.
    """
    n_rows = len(rows) + 1
    row_h = Emu(int(min(Inches(0.34), (bottom - top) / n_rows)))
    # PowerPoint enforces a minimum row height from the font size, so shrink the
    # type rather than let it push the table past the footer.
    max_size = int(row_h / Emu(12700) * 0.62)  # Emu(12700) == 1pt
    size = min(size, max(9, max_size))

    total_w = Inches(sum(widths))
    shape = slide.shapes.add_table(
        n_rows, len(headers), MARGIN, top, total_w, row_h * n_rows
    )
    tbl = shape.table
    tbl.first_row = False
    tbl.horz_banding = False
    for i, w in enumerate(widths):
        tbl.columns[i].width = Inches(w)
    for r in range(n_rows):
        tbl.rows[r].height = row_h

    def style(cell, value: str, *, bold: bool, color: RGBColor, fill: RGBColor, right: bool):
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
        clear_borders(cell)
        cell.margin_left = cell.margin_right = Inches(0.1)
        cell.margin_top = cell.margin_bottom = 0
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf = cell.text_frame
        tf.word_wrap = False
        p = tf.paragraphs[0]
        p.text = value
        p.alignment = PP_ALIGN.RIGHT if right else PP_ALIGN.LEFT
        p.font.name = FONT
        p.font.size = Pt(size)
        p.font.bold = bold
        p.font.color.rgb = color

    lefts = left_cols or set()

    def is_right(c: int) -> bool:
        return c > 0 and c not in lefts

    for c, head in enumerate(headers):
        style(
            tbl.cell(0, c), head.upper(),
            bold=True, color=MUTED, fill=INK, right=is_right(c),
        )
    for r, row in enumerate(rows, start=1):
        fill = PANEL if r % 2 else RULE
        row_color = (highlight or {}).get(r - 1)
        for c, value in enumerate(row):
            # a whole-row highlight wins; otherwise a column can carry its own
            # colour (e.g. every points cell red) and the rest stays neutral
            color = row_color or (col_colors or {}).get(c, WHITE)
            style(
                tbl.cell(r, c), value,
                bold=(c == 0 or color is not WHITE),
                color=color,
                fill=fill,
                right=is_right(c),
            )
    return shape


def footer(slide, week: int, index: int):
    rect(slide, MARGIN, Inches(6.78), FULL_W, Inches(0.012), RULE)
    text(
        slide, MARGIN, Inches(6.95), FULL_W, Inches(0.3),
        f"Chace's 41st League  ·  Week {week}", size=11, color=MUTED,
    )
    text(
        slide, SLIDE_W - MARGIN - Inches(1.0), Inches(6.95), Inches(1.0), Inches(0.3),
        str(index), size=11, color=MUTED, align=PP_ALIGN.RIGHT,
    )


def player(row: Row) -> str:
    return f"{row.name} ({row.pos}, {row.nfl_team})"


def byline(team, detail: str) -> str:
    """Manager + detail, dropping the manager when it just repeats the headline.

    Most of this league never set a team name, so team_name falls back to the
    display name and the two are identical.
    """
    if team.manager and team.manager != team.name:
        return f"{team.manager}  ·  {detail}"
    return detail


# -- slides -----------------------------------------------------------------


def title_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    rect(slide, Inches(0), Inches(0), SLIDE_W, Inches(0.16), GOLD)
    text(
        slide, MARGIN, Inches(2.1), FULL_W, Inches(0.5),
        f"WEEK {a.week}  ·  THE AWARDS", size=18, color=GOLD, bold=True,
    )
    text(
        slide, MARGIN, Inches(2.75), FULL_W, Inches(1.8),
        "Chace's 41st League", size=72, color=WHITE, bold=True, spacing=0.9,
    )
    text(
        slide, MARGIN, Inches(4.5), Inches(9.5), Inches(1.0),
        "Twelve teams. Six games. A lot of decisions that will not be repeated.",
        size=20, color=MUTED,
    )
    text(
        slide, MARGIN, Inches(5.6), Inches(9.5), Inches(0.5),
        f"League average: {fmt(a.league_average)} points",
        size=16, color=GOLD, bold=True,
    )


def champ_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    t = a.champ
    y = header(
        slide, "🏆 Team of the Week", t.name,
        byline(t, f"beat {t.opponent} by {fmt(abs(t.margin))}"),
    )
    stat_block(slide, fmt(t.points), "points")
    text(slide, MARGIN, y, BODY_W, Inches(0.4), "Carried by", size=14, color=MUTED, bold=True)
    bullets(
        slide, y + Inches(0.45),
        [f"{player(r)} — {fmt(r.points)}  ({r.slot})" for r in a.champ_top],
    )


def chump_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    t, champ = a.chump, a.champ
    y = header(
        slide, "💀 Team of the Weak", t.name,
        byline(t, f"{fmt(champ.points - t.points)} behind the league leader"),
    )
    stat_block(slide, fmt(t.points), "points", RED)
    text(slide, MARGIN, y, BODY_W, Inches(0.4), "The lowlights", size=14, color=MUTED, bold=True)
    end = bullets(
        slide, y + Inches(0.45),
        [f"{player(r)} — {fmt(r.points)}  ({r.slot})" for r in a.chump_duds],
    )
    heroes = " and ".join(f"{r.name} ({fmt(r.points)})" for r in a.chump_heroes)
    text(
        slide, MARGIN, end + Inches(0.15), BODY_W, Inches(0.8),
        f"In fairness, {heroes} showed up.\nEveryone else filed a police report.",
        size=15, color=MUTED,
    )


def overachiever_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    top = a.overachievers[0]
    y = header(
        slide, "📈 Biggest Overachiever", player(top),
        f"Projected {fmt(top.projected or 0)}  ·  started by {top.team}",
    )
    stat_block(slide, signed(top.vs_proj or 0), "vs. projection", GREEN)
    runners_up(
        slide, y,
        [
            f"{player(r)} — {fmt(r.projected or 0)} projected → {fmt(r.points)} "
            f"({signed(r.vs_proj or 0)}), {r.team}"
            for r in a.overachievers[1:]
        ],
        label="Also beat their number",
    )
    if a.benched_overachiever:
        r = a.benched_overachiever
        text(
            slide, MARGIN, Inches(5.5), BODY_W, Inches(0.9),
            f"And the one nobody got to enjoy: {r.name} beat his projection by "
            f"{signed(r.vs_proj or 0)} — from {r.team}'s bench.",
            size=15, color=GOLD,
        )


def bust_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    top = a.busts[0]
    y = header(
        slide, "📉 Biggest Bust", player(top),
        f"Projected {fmt(top.projected or 0)}  ·  started by {top.team}",
    )
    stat_block(slide, signed(top.vs_proj or 0), "vs. projection", RED)
    runners_up(
        slide, y,
        [
            f"{player(r)} — {fmt(r.projected or 0)} projected → {fmt(r.points)} "
            f"({signed(r.vs_proj or 0)}), {r.team}"
            for r in a.busts[1:]
        ],
        label="Also let somebody down",
    )


def late_round_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    top = a.late_steals[0]
    seat = f"started by {top.team}" if top.started else f"on {top.team}'s bench"
    y = header(
        slide, f"💎 Late-Round Steal · rounds {LATE_ROUND_START}+", player(top),
        f"Taken {drafted_note(top)}  ·  {seat}",
    )
    stat_block(slide, fmt(top.points), "points")
    runners_up(
        slide, y,
        [
            f"{player(r)} — {fmt(r.points)}, {drafted_note(r)} · "
            + (f"started by {r.team}" if r.started else f"{r.team}'s bench")
            for r in a.late_steals[1:]
        ],
        label="The rest of the bargain bin",
    )


def late_started_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    if not a.late_steals_started:
        header(
            slide, "🎯 Late-Round Steal Somebody Started", "Nobody.",
            "Not one manager trusted a late-round pick. Cowards.",
        )
        return
    top = a.late_steals_started[0]
    y = header(
        slide, "🎯 Late-Round Steal Somebody Started", player(top),
        f"{drafted_note(top)}  ·  started by {top.team}",
    )
    stat_block(slide, fmt(top.points), "points")
    runners_up(
        slide, y,
        [
            f"{player(r)} — {fmt(r.points)} from {drafted_note(r)} · {r.team}"
            for r in a.late_steals_started[1:]
        ],
        label="Others who trusted a late pick",
    )


def bench_burner_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    top = a.blunders[0]
    y = header(
        slide, "🔥 The Bench Burner", top.team.name,
        f"Started {top.started.name} ({fmt(top.started.points)}) over "
        f"{top.benched.name} ({fmt(top.benched.points)})",
    )
    stat_block(slide, f"−{fmt(top.gap)}", "points left on the bench", RED)
    runners_up(
        slide, y,
        [
            f"{b.team.name}: started {b.started.name} ({fmt(b.started.points)}) "
            f"over {b.benched.name} ({fmt(b.benched.points)}) — −{fmt(b.gap)}"
            for b in a.blunders[1:]
        ],
        label="Not alone",
    )
    if top.gap > abs(top.team.margin) and not top.team.won:
        text(
            slide, MARGIN, Inches(5.5), BODY_W, Inches(0.9),
            f"{top.team.name} lost by {fmt(abs(top.team.margin))}. "
            f"That one decision was the whole game.",
            size=16, color=GOLD, bold=True,
        )


def efficiency_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    best, worst = a.efficiency[0], a.efficiency[-1]
    header(
        slide, "🧠 Manager Efficiency",
        "Points scored vs. points available",
        f"Sharpest: {best.team.name} ({best.pct:.0f}%)  ·  "
        f"Roughest: {worst.team.name} ({worst.pct:.0f}%)",
    )
    highlight = {0: GREEN, len(a.efficiency) - 1: RED}
    table(
        slide,
        ["Team", "Actual", "Optimal", "%"],
        [
            [e.team.name, fmt(e.team.points), fmt(e.optimal), f"{e.pct:.0f}%"]
            for e in a.efficiency
        ],
        top=Inches(2.85),
        widths=[5.4, 1.5, 1.5, 1.2],
        highlight=highlight,
    )


def luck_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    y = header(slide, "😤 Luckiest & Unluckiest", "The scoreboard is not fair")
    lines = []
    if a.unlucky:
        beaten = sum(1 for t in a.teams if t.points < a.unlucky.points)
        lines.append(
            f"UNLUCKIEST — {a.unlucky.name} scored {fmt(a.unlucky.points)}, more than "
            f"{beaten} other teams, and still lost to {a.unlucky.opponent} "
            f"({fmt(a.unlucky.opponent_points)})."
        )
    if a.lucky:
        lines.append(
            f"LUCKIEST — {a.lucky.name} won with just {fmt(a.lucky.points)}, drawing "
            f"{a.lucky.opponent} ({fmt(a.lucky.opponent_points)}). A win is a win. Barely."
        )
    bullets(slide, y, lines, size=17, width=FULL_W)


def margins_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)

    def scoreline(t) -> str:
        """Winner first, so it reads like a box score."""
        if t.won:
            return f"{t.name} {fmt(t.points)} — {fmt(t.opponent_points)} {t.opponent}"
        return f"{t.opponent} {fmt(t.opponent_points)} — {fmt(t.points)} {t.name}"

    y = header(slide, "⚔️ Closest Game & Biggest Blowout", "Two very different afternoons")
    bullets(
        slide, y,
        [
            f"NAIL-BITER — {scoreline(a.closest)}  (margin {fmt(abs(a.closest.margin))})",
            f"BEATDOWN — {scoreline(a.blowout)}  (margin {fmt(abs(a.blowout.margin))}). "
            f"Mercy rule, please.",
        ],
        size=17, width=FULL_W,
    )


def zero_club_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    header(
        slide, "🥚 The Zero Club",
        f"Started, scored {fmt(ZERO_CLUB_THRESHOLD)} or fewer",
        f"{len(a.zero_club)} players took the field and did essentially nothing.",
    )
    table(
        slide,
        ["Player", "Pos", "Points", "Started by"],
        [[r.name, r.pos, fmt(r.points), r.team] for r in a.zero_club],
        top=Inches(2.85),
        widths=[4.0, 1.0, 1.4, 5.1],
        col_colors={1: MUTED, 2: RED, 3: MUTED},
        left_cols={3},
    )


def undrafted_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    if not a.undrafted:
        header(
            slide, "🦄 Free Money", "Nothing to see here",
            "Every starter in the league was drafted. A very boring, very safe week.",
        )
        return
    top = a.undrafted[0]
    y = header(
        slide, "🦄 Free Money · undrafted, and started anyway", player(top),
        f"Nobody spent a pick on him. {top.team} picked him up off the floor.",
    )
    stat_block(slide, fmt(top.points), "points")
    runners_up(
        slide, y,
        [f"{player(r)} — {fmt(r.points)} · {r.team}" for r in a.undrafted[1:]],
        label="Also free",
    )


def projections_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    total = len(a.vs_projection)
    header(
        slide, "📊 The League vs. The Projections",
        f"{a.beat_projection} of {total} teams beat their number",
        f"The other {total - a.beat_projection} of us are still calling it 'a process'.",
    )
    highlight = {
        i: (GREEN if t.points > (t.projected or 0) else RED)
        for i, t in enumerate(a.vs_projection)
    }
    table(
        slide,
        ["Team", "Projected", "Actual", "Diff"],
        [
            [
                t.name, fmt(t.projected or 0), fmt(t.points),
                signed(t.points - (t.projected or 0)),
            ]
            for t in a.vs_projection
        ],
        top=Inches(2.85),
        widths=[5.4, 1.6, 1.4, 1.2],
        highlight=highlight,
    )


def positional_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    header(
        slide, "🤡 Positional High Scores", "Best started player at every slot",
        "Yes, a kicker made the list. It happens every year.",
    )
    table(
        slide,
        ["Slot", "Player", "Points", "Team"],
        [[pos, r.name, fmt(r.points), r.team] for pos, r in a.positional_best],
        top=Inches(3.0),
        widths=[1.1, 4.0, 1.4, 5.0],
        col_colors={2: GOLD, 3: MUTED},
        left_cols={1, 3},
        size=14,
    )


def closer_slide(prs: Presentation, a: Awards):
    slide = new_slide(prs)
    y = header(slide, "📣 If you only read one thing", f"Week {a.week}, in four lines")
    lines = [
        f"{a.champ.name} dropped {fmt(a.champ.points)} and looked unbeatable.",
        f"{a.chump.name} managed {fmt(a.chump.points)} and looked unrecognizable.",
    ]
    if a.blunders:
        top = a.blunders[0]
        lines.append(
            f"{top.team.name} left {fmt(top.gap)} points on the bench in a single "
            f"slot — the week's most expensive click."
        )
    if a.unlucky:
        lines.append(
            f"Spare a thought for {a.unlucky.name}, who scored "
            f"{fmt(a.unlucky.points)} and lost anyway."
        )
    bullets(slide, y, lines, size=18, width=FULL_W)
    text(
        slide, MARGIN, Inches(5.9), FULL_W, Inches(0.6),
        f"See you in week {a.week + 1}.", size=22, color=GOLD, bold=True,
    )


LEFT_COL = MARGIN
RIGHT_COL = Inches(6.95)


def high_low_slide(prs: Presentation, a: Awards):
    """Team of the Week and Team of the Weak are one story -- the spread."""
    slide = new_slide(prs)
    champ, chump = a.champ, a.chump
    header(slide, "🏆 High & Low", "The best and worst afternoons in the league")
    panel(
        slide, LEFT_COL,
        label="🏆 Team of the Week",
        label_color=GOLD,
        title=champ.name,
        stat=fmt(champ.points),
        lines=[f"{r.name} — {fmt(r.points)}" for r in a.champ_top]
        + [f"Beat {champ.opponent} by {fmt(abs(champ.margin))}"],
    )
    panel(
        slide, RIGHT_COL,
        label="💀 Team of the Weak",
        label_color=RED,
        title=chump.name,
        stat=fmt(chump.points),
        lines=[f"{r.name} — {fmt(r.points)}" for r in a.chump_duds]
        # kept short deliberately: at 13pt this panel fits ~55 characters a line
        + ["Bright spots: " + ", ".join(r.name for r in a.chump_heroes)],
    )
    if a.unlucky:
        beaten = sum(1 for t in a.teams if t.points < a.unlucky.points)
        text(
            slide, MARGIN, Inches(6.45), FULL_W, Inches(0.4),
            f"Meanwhile {a.unlucky.name} scored {fmt(a.unlucky.points)} — more than "
            f"{beaten} other teams — and still lost to {a.unlucky.opponent}.",
            size=14, color=GOLD,
        )


def hero_villain_slide(prs: Presentation, a: Awards):
    """Biggest beat and biggest miss: same shape, opposite sign."""
    slide = new_slide(prs)
    hero, villain = a.overachievers[0], a.busts[0]
    header(slide, "📈 Hero & Villain", "Best beat. Worst miss.")
    panel(
        slide, LEFT_COL,
        label="📈 Biggest Overachiever",
        label_color=GREEN,
        title=player(hero),
        stat=signed(hero.vs_proj or 0),
        lines=[
            f"Projected {fmt(hero.projected or 0)} → scored {fmt(hero.points)}",
            f"Started by {hero.team}",
        ],
    )
    panel(
        slide, RIGHT_COL,
        label="📉 Biggest Bust",
        label_color=RED,
        title=player(villain),
        stat=signed(villain.vs_proj or 0),
        lines=[
            f"Projected {fmt(villain.projected or 0)} → scored {fmt(villain.points)}",
            f"Started by {villain.team}",
        ],
    )
    if a.benched_overachiever:
        r = a.benched_overachiever
        text(
            slide, MARGIN, Inches(6.45), FULL_W, Inches(0.4),
            f"And the one nobody got to enjoy: {r.name} beat his projection by "
            f"{signed(r.vs_proj or 0)} — from {r.team}'s bench.",
            size=14, color=GOLD,
        )


def bargain_bin_slide(prs: Presentation, a: Awards):
    """Late-round picks and waiver pickups are the same joke: cheap player, big score."""
    slide = new_slide(prs)
    rows = []
    for r in a.late_steals:
        seat = "started" if r.started else "benched"
        rows.append(
            [r.name, f"R{r.draft_round} · pick {r.draft_pick}", fmt(r.points), r.team, seat]
        )
    for r in a.undrafted:
        rows.append([r.name, "undrafted", fmt(r.points), r.team, "started"])

    header(
        slide, f"💎 The Bargain Bin", "Round 9 or later, or never drafted at all",
        "The cheapest players on the board outscored a lot of expensive ones.",
    )
    table(
        slide,
        ["Player", "Drafted", "Points", "Team", ""],
        rows,
        top=Inches(3.0),
        widths=[3.3, 2.3, 1.3, 3.5, 1.1],
        col_colors={1: MUTED, 2: GOLD, 3: MUTED, 4: MUTED},
        left_cols={1, 3, 4},
        size=14,
    )


def build_deck(a: Awards, full: bool = False) -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = SLIDE_W, SLIDE_H

    title_slide(prs, a)
    if full:
        builders = [champ_slide, chump_slide]
        if a.have_projections:
            builders += [overachiever_slide, bust_slide]
        if a.have_draft:
            builders += [late_round_slide, late_started_slide]
        builders += [bench_burner_slide, efficiency_slide, luck_slide, margins_slide]
        if a.zero_club:
            builders.append(zero_club_slide)
        if a.have_draft:
            builders.append(undrafted_slide)
        if a.have_projections and a.vs_projection:
            builders.append(projections_slide)
        builders += [positional_slide, closer_slide]
    else:
        # Standup cut: every slide either puts someone in the room on screen or
        # gets a laugh. Merged pairs beat separate slides that tell one story.
        builders = [high_low_slide]
        if a.have_projections:
            builders.append(hero_villain_slide)
        builders += [bench_burner_slide, efficiency_slide]
        if a.zero_club:
            builders.append(zero_club_slide)
        if a.have_draft:
            builders.append(bargain_bin_slide)

    for build in builders:
        build(prs, a)

    # title slide carries no footer
    for i, slide in enumerate(list(prs.slides)[1:], start=2):
        footer(slide, a.week, i)
    return prs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: data/awards_week_N.pptx"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="every award on its own slide (~16) instead of the 7-slide standup cut",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    awards = load_awards(settings, args.week)

    out_path = args.out or (settings.data_dir / f"awards_week_{args.week}.pptx")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    build_deck(awards, full=args.full).save(str(out_path))
    logger.info("Wrote %s", out_path)


if __name__ == "__main__":
    main()
