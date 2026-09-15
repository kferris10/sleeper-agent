"""A playful, shareable weekly league recap ("The Chace's 41st Awards").

Prints a markdown award show to stdout and writes data/recap_week_N.md. The
rendering itself lives in sleeper_analyst.report.render_awards_markdown, which
the weekly email also uses -- this script is just a standalone way to get the
same section without running the whole pipeline.

    uv run python scripts/league_recap.py --week 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sleeper_analyst.awards import load_awards
from sleeper_analyst.config import load_settings
from sleeper_analyst.report import render_awards_markdown

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--week", type=int, default=None,
        help="completed week to award (default: the most recent one)",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: data/recap_week_N.md"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    awards = load_awards(settings, args.week)

    markdown = "\n".join(
        [
            f"# 🏈 Week {awards.week} Awards — Chace's 41st League",
            "",
            render_awards_markdown(awards),
        ]
    )

    out_path = args.out or (settings.data_dir / f"recap_week_{awards.week}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("Wrote %s", out_path)

    # The Windows console defaults to cp1252 and chokes on the emoji headers.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(markdown)


if __name__ == "__main__":
    main()
