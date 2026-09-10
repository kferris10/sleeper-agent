"""CLI: `sleeper-analyst setup|collect|analyze|deliver|run`."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from sleeper_analyst.collect import build_weekly_context
from sleeper_analyst.config import load_settings
from sleeper_analyst.sleeper.client import SleeperClient
from sleeper_analyst.sleeper.players import PlayerCache

logger = logging.getLogger(__name__)


def cmd_setup(args: argparse.Namespace) -> int:
    with SleeperClient() as client:
        user = client.get_user(args.username)
        season = args.season or client.get_state().season
        leagues = client.get_user_leagues(user.user_id, season)

    print(f"user_id: {user.user_id}  (username: {user.username or args.username})")
    if not leagues:
        print(f"No NFL leagues found for season {season}.")
        return 1
    print(f"\nLeagues for {season}:")
    for lg in leagues:
        print(f"  {lg.league_id}  {lg.name}  ({lg.total_rosters} teams, waivers: {lg.waiver_type})")
    print(
        "\nCopy config.example.toml to config.toml and set [league] league_id and "
        f'user_id = "{user.user_id}"'
    )
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    if not settings.league.league_id or not settings.league.user_id:
        print(
            "config.toml is missing [league] league_id/user_id. "
            "Run: sleeper-analyst setup --username YOUR_SLEEPER_NAME",
            file=sys.stderr,
        )
        return 2

    with SleeperClient() as client:
        players = PlayerCache(settings.data_dir).get(client, force=args.refresh_players)
        context = build_weekly_context(client, players, settings, week=args.week)

    out = args.out or settings.data_dir / f"context_week_{context.upcoming_week}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(context.model_dump_json(indent=2), encoding="utf-8")

    free_agent_count = sum(len(v) for v in context.free_agents.values())
    size_kb = out.stat().st_size / 1024
    print(f"Wrote {out} ({size_kb:.0f} KB)")
    print(
        f"  season {context.season}, completed week {context.completed_week}, "
        f"upcoming week {context.upcoming_week}"
    )
    print(
        f"  my team: {context.my_team.record}, {len(context.my_team.players)} players; "
        f"{len(context.other_teams)} other teams; {free_agent_count} free agents; "
        f"{len(context.league_activity)} recent transactions"
    )
    if not any(p.bye_week for p in context.my_team.players):
        print("  note: no bye weeks resolved — check the [byes] table in config.toml")
    return 0


def _latest_context(data_dir: Path) -> Path | None:
    candidates = sorted(
        data_dir.glob("context_week_*.json"),
        key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)),
    )
    return candidates[-1] if candidates else None


def cmd_analyze(args: argparse.Namespace) -> int:
    from sleeper_analyst.analyze import AnalysisError, run_analysis
    from sleeper_analyst.sleeper.models import WeeklyContext
    from sleeper_analyst.store import Store

    settings = load_settings(args.config)
    context_path = args.context or _latest_context(settings.data_dir)
    if context_path is None or not context_path.exists():
        print(
            f"No context file found ({context_path or settings.data_dir / 'context_week_*.json'}). "
            "Run: sleeper-analyst collect",
            file=sys.stderr,
        )
        return 2

    context = WeeklyContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    print(f"Analyzing {context_path} (week {context.upcoming_week}, model {settings.claude.model})")

    with Store(settings.data_dir / "history.db") as store:
        # Feed last week's analysis back in so Claude can self-grade
        prior = store.get_analysis(context.season, context.completed_week)
        if prior:
            context.prior_analysis = json.loads(prior)

        try:
            analysis, raw = run_analysis(context, settings)
        except AnalysisError as exc:
            print(f"Analysis failed after retry: {exc}", file=sys.stderr)
            return 1

        store.save_context(context.season, context.upcoming_week, context.model_dump_json())
        store.save_analysis(
            context.season, context.upcoming_week, settings.claude.model,
            analysis.model_dump_json(), raw,
        )

    out = args.out or settings.data_dir / f"analysis_week_{context.upcoming_week}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote {out}\n")

    print(f"RECAP: {analysis.recap.summary}\n")
    print("LINEUP:")
    for rec in analysis.lineup:
        print(f"  {rec.slot:<10} {rec.player:<24} {rec.reason}")
    if analysis.bench:
        print("BENCH:")
        for b in analysis.bench:
            print(f"  {b.player:<24} {b.reason}")
    if analysis.waivers:
        print("WAIVERS (in priority order):")
        for w in analysis.waivers:
            bid = f" bid ${w.faab_bid}" if w.faab_bid is not None else ""
            drop = f", drop {w.drop}" if w.drop else ""
            print(f"  {w.priority}. add {w.add}{drop}{bid} — {w.reason}")
    if analysis.trades:
        print("TRADE IDEAS:")
        for t in analysis.trades:
            print(f"  to {t.partner}: give {', '.join(t.give)} / get {', '.join(t.get)}")
            print(f"    pitch: {t.pitch}")
    if analysis.watchlist:
        print("WATCHLIST:")
        for wl in analysis.watchlist:
            print(f"  {wl.player}: {wl.note}")
    if analysis.confidence_notes:
        print(f"\nCONFIDENCE: {analysis.confidence_notes}")
    return 0


FRIDAY_DIRECTIVE = (
    "This is the FRIDAY MORNING pre-game update, not the main Tuesday packet. Waivers for "
    "this week have already processed and the Tuesday packet was already delivered. Re-check "
    "the week's practice reports and any injury designations announced so far for every "
    "recommended starter and their direct backups, and produce the final lineup. Only "
    "recommend free-agent adds or trades if injury news makes one urgent."
)


def _write_packet(settings, context, analysis, tag: str = "") -> tuple[Path, Path, str, str, str]:
    """Render analysis → (md_path, html_path, subject, markdown, html)."""
    from sleeper_analyst.report import render_html, render_markdown, subject_line

    markdown = render_markdown(analysis, context)
    html = render_html(analysis, context)
    md_path = settings.data_dir / f"packet_week_{context.upcoming_week}.md"
    html_path = settings.data_dir / f"packet_week_{context.upcoming_week}.html"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")
    return md_path, html_path, subject_line(context, tag), markdown, html


def cmd_deliver(args: argparse.Namespace) -> int:
    from sleeper_analyst.deliver import DeliveryError, get_deliverer
    from sleeper_analyst.sleeper.models import Analysis, WeeklyContext
    from sleeper_analyst.store import Store

    settings = load_settings(args.config)
    analysis_path = args.analysis
    if analysis_path is None:
        candidates = sorted(
            settings.data_dir.glob("analysis_week_*.json"),
            key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)),
        )
        analysis_path = candidates[-1] if candidates else None
    if analysis_path is None or not analysis_path.exists():
        print("No analysis file found. Run: sleeper-analyst analyze", file=sys.stderr)
        return 2

    week = int(re.search(r"(\d+)", analysis_path.stem).group(1))
    context_path = settings.data_dir / f"context_week_{week}.json"
    if not context_path.exists():
        print(f"Matching context {context_path} not found.", file=sys.stderr)
        return 2

    analysis = Analysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
    context = WeeklyContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    md_path, html_path, subject, markdown, html = _write_packet(settings, context, analysis)
    print(f"Wrote {md_path} and {html_path}")

    if args.dry_run:
        print("Dry run: not sending.")
        return 0
    try:
        deliverer = get_deliverer(settings)
        deliverer.send(subject, markdown, html)
    except DeliveryError as exc:
        print(f"Delivery failed: {exc}", file=sys.stderr)
        return 1
    with Store(settings.data_dir / "history.db") as store:
        store.mark_delivered(context.season, context.upcoming_week, deliverer.channel, "manual")
    print(f"Delivered via {deliverer.channel}: {subject}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """collect → analyze → store → deliver, idempotent per week."""
    from sleeper_analyst.deliver import get_deliverer

    settings = load_settings(args.config)
    try:
        return _run_pipeline(settings, args)
    except Exception as exc:  # noqa: BLE001 — anything fatal triggers the failure alert
        logger.exception("run failed")
        try:
            get_deliverer(settings).send(
                "sleeper-analyst run FAILED",
                f"The weekly run failed with:\n\n{type(exc).__name__}: {exc}\n\n"
                "Check the logs (GitHub Actions run or terminal output).",
                f"<p>The weekly run failed with:</p><pre>{type(exc).__name__}: {exc}</pre>",
            )
            print("Failure alert sent.", file=sys.stderr)
        except Exception as alert_exc:  # noqa: BLE001
            print(f"Failure alert also failed: {alert_exc}", file=sys.stderr)
        return 1


def _run_pipeline(settings, args: argparse.Namespace) -> int:
    from sleeper_analyst.analyze import run_analysis
    from sleeper_analyst.deliver import get_deliverer
    from sleeper_analyst.store import Store

    tag = args.tag
    with SleeperClient() as client:
        players = PlayerCache(settings.data_dir).get(client)
        context = build_weekly_context(client, players, settings, week=args.week)
    if tag == "friday":
        context.directive = FRIDAY_DIRECTIVE

    context_path = settings.data_dir / f"context_week_{context.upcoming_week}.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(context.model_dump_json(indent=2), encoding="utf-8")
    print(f"Collected week {context.upcoming_week} → {context_path}")

    with Store(settings.data_dir / "history.db") as store:
        if store.was_delivered(context.season, context.upcoming_week, tag) and not args.force:
            print(
                f"Week {context.upcoming_week} ({tag}) was already delivered; skipping "
                "(use --force to re-run)."
            )
            return 0

        # Feed the most recent analysis back in: last week's for the Tuesday
        # packet, this week's Tuesday packet for the Friday re-check.
        prior = store.get_analysis(context.season, context.upcoming_week if tag == "friday" else context.completed_week)
        if prior is None and tag == "friday":
            prior = store.get_analysis(context.season, context.completed_week)
        if prior:
            context.prior_analysis = json.loads(prior)

        print(f"Analyzing with {settings.claude.model}…")
        analysis, raw = run_analysis(context, settings)
        store.save_context(context.season, context.upcoming_week, context.model_dump_json())
        store.save_analysis(
            context.season, context.upcoming_week, settings.claude.model,
            analysis.model_dump_json(), raw,
        )

        analysis_path = settings.data_dir / f"analysis_week_{context.upcoming_week}.json"
        analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
        md_path, html_path, subject, markdown, html = _write_packet(settings, context, analysis, tag)
        print(f"Wrote {analysis_path}, {md_path}, {html_path}")

        deliverer = get_deliverer(settings)
        deliverer.send(subject, markdown, html)
        store.mark_delivered(context.season, context.upcoming_week, deliverer.channel, tag)
        print(f"Delivered via {deliverer.channel}: {subject}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # ANTHROPIC_API_KEY may live in .env
    parser = argparse.ArgumentParser(prog="sleeper-analyst")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="look up your Sleeper user_id and league ids")
    p_setup.add_argument("--username", required=True, help="your Sleeper username")
    p_setup.add_argument("--season", help="season year (default: current)")
    p_setup.set_defaults(func=cmd_setup)

    p_collect = sub.add_parser("collect", help="build the WeeklyContext JSON")
    p_collect.add_argument("--week", type=int, help="upcoming week (default: current from /state/nfl)")
    p_collect.add_argument("--out", type=Path, help="output path (default: data/context_week_N.json)")
    p_collect.add_argument("--config", type=Path, help="path to config.toml (default: ./config.toml)")
    p_collect.add_argument(
        "--refresh-players", action="store_true",
        help="request a player-map refetch (still refused inside the 24h TTL)",
    )
    p_collect.set_defaults(func=cmd_collect)

    p_analyze = sub.add_parser("analyze", help="run Claude on a collected context")
    p_analyze.add_argument(
        "--context", type=Path,
        help="path to a context_week_N.json (default: newest in data/)",
    )
    p_analyze.add_argument("--out", type=Path, help="output path (default: data/analysis_week_N.json)")
    p_analyze.add_argument("--config", type=Path, help="path to config.toml (default: ./config.toml)")
    p_analyze.set_defaults(func=cmd_analyze)

    p_deliver = sub.add_parser("deliver", help="render and send an existing analysis")
    p_deliver.add_argument(
        "--analysis", type=Path,
        help="path to an analysis_week_N.json (default: newest in data/)",
    )
    p_deliver.add_argument("--config", type=Path, help="path to config.toml (default: ./config.toml)")
    p_deliver.add_argument(
        "--dry-run", action="store_true",
        help="write packet_week_N.md/.html but do not send",
    )
    p_deliver.set_defaults(func=cmd_deliver)

    p_run = sub.add_parser(
        "run", help="collect → analyze → deliver (skips weeks already delivered)"
    )
    p_run.add_argument("--week", type=int, help="upcoming week (default: current from /state/nfl)")
    p_run.add_argument("--config", type=Path, help="path to config.toml (default: ./config.toml)")
    p_run.add_argument("--force", action="store_true", help="re-run even if already delivered")
    p_run.add_argument(
        "--tag", default="tuesday", choices=["tuesday", "friday"],
        help="run type: tuesday = full packet, friday = injury re-check (default: tuesday)",
    )
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
