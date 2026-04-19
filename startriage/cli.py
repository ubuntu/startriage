"""CLI argument parsing for startriage."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from startriage.config import load_config
from startriage.dates import parse_interval
from startriage.output import OutputFormat
from startriage.triage import TriageRunOptions, resolve_sources, run_todo, run_triage


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="startriage",
        description="Unified triage tool for Ubuntu bugs, documentation, and forum posts.",
    )

    # Global options
    parser.add_argument(
        "-t",
        "--team",
        metavar="TEAM",
        help="Team name to triage (defaults to the only configured team, or general.default_team)",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="PATH",
        type=Path,
        help="Path to config TOML (default: ~/.config/startriage.toml)",
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "-o", "--open", action="store_true", dest="open_in_browser", help="Open items in web browser"
    )
    parser.add_argument("--fullurls", action="store_true", help="Show full URLs instead of hyperlinks")

    # Shared parent parser for subcommands that support --markdown output
    markdown_parser = argparse.ArgumentParser(add_help=False)
    markdown_parser.add_argument(
        "--markdown",
        metavar="PATH",
        help="Write parallel markdown output to PATH (for Discourse post template)",
    )

    sp = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- list ---
    list_parser = sp.add_parser("list", help="List triage items")
    list_sub = list_parser.add_subparsers(dest="list_command", metavar="SUBCOMMAND")

    # list triage (also the default when no subcommand given)
    triage_parser = list_sub.add_parser("triage", help="Daily triage (default)", parents=[markdown_parser])
    _add_triage_args(triage_parser)

    # list todo
    todo_parser = list_sub.add_parser("todo", help="Tagged bug housekeeping", parents=[markdown_parser])
    _add_todo_args(todo_parser)

    # --- forum ---
    forum_parser = sp.add_parser("forum", help="Discourse forum commands")
    forum_sub = forum_parser.add_subparsers(dest="forum_command", metavar="SUBCOMMAND")
    backlog_parser = forum_sub.add_parser(
        "backlog", help="Print a single post in backlog format", parents=[markdown_parser]
    )
    backlog_parser.add_argument("post_id", type=int, help="Discourse post ID")
    backlog_parser.add_argument("-s", "--site", help="Discourse site URL")

    # --- config ---
    config_parser = sp.add_parser("config", help="Manage configuration")
    config_sub = config_parser.add_subparsers(dest="config_command")
    set_parser = config_sub.add_parser("set-defaults", help="Persist site/category to config file")
    set_parser.add_argument("-s", "--site", help="Discourse site URL")
    set_parser.add_argument("--category", help="Discourse category")
    set_parser.add_argument("--default-team", help="Set general.default_team in config")

    return parser


def _add_triage_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-i",
        "--interval",
        default=None,
        metavar="DATE[:DATE]",
        help="Date interval: YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, or day name (e.g. monday)",
    )
    p.add_argument(
        "--source",
        default=None,
        metavar="SOURCE[,SOURCE]",
        help="Comma-separated sources to include: launchpad/bugs, discourse/forum, github/docs",
    )
    p.add_argument("--no-expiration", action="store_true", help="Skip expiring bugs subsection")
    p.add_argument("--expire-tagged", type=int, metavar="DAYS")
    p.add_argument("--expire", type=int, metavar="DAYS")
    p.add_argument("--flag-recent", type=int, metavar="DAYS")
    p.add_argument("--flag-old", type=int, metavar="DAYS")
    p.add_argument("--no-ignore-list", action="store_true", help="Include normally-ignored packages")
    p.add_argument(
        "--update",
        choices=["theirs", "ours", "all"],
        default=None,
        help="Filter by who last updated bugs (default: theirs)",
    )


def _add_todo_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--subscribed",
        action="store_true",
        help="Show subscription backlog (directly subscribed, tag excluded)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --subscribed: show only top and bottom N bugs",
    )
    p.add_argument("--no-save", action="store_true", help="Do not save bug list to file")
    p.add_argument("-S", "--save", metavar="PATH", help="Override auto save path")
    p.add_argument("-C", "--compare", metavar="PATH", help="Override auto compare path")
    p.add_argument("-P", "--postponed", metavar="PATH", help="Override auto postponed path")
    p.add_argument("--json", action="store_true", dest="json_output", help="Print JSON output")
    p.add_argument("--flag-recent", type=int, default=None, metavar="DAYS")
    p.add_argument("--flag-old", type=int, default=None, metavar="DAYS")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        format="%(message)s",
        level=logging.DEBUG if debug else logging.INFO,
    )


def _resolve_team_name(team_arg: str | None, config) -> str:
    """Determine which team to use.

    Priority:
    1. Explicit -t/--team argument
    2. general.default_team in config
    3. If exactly one team is configured, use it automatically
    """
    if team_arg:
        return team_arg
    default = getattr(config.general, "default_team", None)
    if default:
        return default
    teams = list(config.team.keys())
    if len(teams) == 1:
        return teams[0]
    available = ", ".join(sorted(teams)) or "(none)"
    raise KeyError(f"Multiple teams configured; use -t to pick one: {available}")


def _make_opts(args: argparse.Namespace, fmt: OutputFormat) -> TriageRunOptions:
    start, end = parse_interval(getattr(args, "interval", None))
    return TriageRunOptions(
        start=start,
        end=end,
        sources=resolve_sources(getattr(args, "source", None)),
        open_in_browser=args.open_in_browser,
        shorten_links=not args.fullurls,
        fmt=fmt,
        markdown_path=getattr(args, "markdown", None),
        update_filter=getattr(args, "update", None),
    )


def main() -> None:
    parser = _build_parser()
    # Allow bare `startriage` and `startriage list` to mean `startriage list triage`
    args = parser.parse_args()

    _setup_logging(args.debug)

    try:
        config = load_config(args.config)
        team_name = _resolve_team_name(args.team, config)
        team = config.get_team(team_name)
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    general = config.general
    # only the --markdown file writes markdown format separately
    fmt = OutputFormat.TERMINAL

    command = args.command
    if command is None or (command == "list" and getattr(args, "list_command", None) in (None, "triage")):
        _run_triage(args, team, general, fmt)
    elif command == "list" and args.list_command == "todo":
        _run_todo(args, team, general, fmt)
    elif command == "forum" and getattr(args, "forum_command", None) == "backlog":
        _run_backlog(args, team, general)
    elif command == "config" and getattr(args, "config_command", None) == "set-defaults":
        _run_set_defaults(args, args.config)
    else:
        parser.print_help()
        sys.exit(1)


def _run_triage(args: argparse.Namespace, team, general, fmt: OutputFormat) -> None:
    from datetime import datetime, timedelta, timezone

    from startriage.sources.launchpad.models import Task

    triage_args = (
        args
        if hasattr(args, "interval")
        else argparse.Namespace(
            interval=None,
            source=None,
            flag_recent=None,
            flag_old=None,
            no_ignore_list=False,
            update=None,
            markdown=None,
        )
    )

    if getattr(triage_args, "flag_recent", None) is not None:
        Task.AGE = datetime.now(timezone.utc) - timedelta(days=triage_args.flag_recent)
    if getattr(triage_args, "flag_old", None) is not None:
        Task.OLD = datetime.now(timezone.utc) - timedelta(days=triage_args.flag_old)
    if getattr(triage_args, "no_ignore_list", False):
        team = team.model_copy(update={"lp_ignore_packages": []})

    opts = _make_opts(triage_args, fmt)

    asyncio.run(run_triage(team, general, opts))


def _run_todo(args: argparse.Namespace, team, general, fmt: OutputFormat) -> None:
    from datetime import datetime, timedelta, timezone

    from startriage.sources.launchpad.models import Task

    if args.flag_recent is not None:
        Task.AGE = datetime.now(timezone.utc) - timedelta(days=args.flag_recent)
    elif not args.subscribed:
        # Default flag-recent=6 for todo mode
        Task.AGE = datetime.now(timezone.utc) - timedelta(days=6)
    if args.flag_old is not None:
        Task.OLD = datetime.now(timezone.utc) - timedelta(days=args.flag_old)

    opts = TriageRunOptions(
        start=None,
        end=None,
        sources=frozenset(["launchpad", "github"]),
        open_in_browser=args.open_in_browser,
        shorten_links=not args.fullurls,
        fmt=fmt,
        markdown_path=getattr(args, "markdown", None),
    )

    asyncio.run(
        run_todo(
            team,
            general,
            opts,
            filename_save=args.save,
            filename_compare=args.compare,
            filename_postponed=args.postponed,
            no_save=args.no_save,
            limit=args.limit,
            subscribed=args.subscribed,
            json_output=args.json_output,
        )
    )


def _run_backlog(args: argparse.Namespace, team, general) -> None:
    import asyncio

    import aiohttp

    from startriage.sources.discourse import finder as df

    site = args.site or general.discourse_site

    async def _fetch():
        async with aiohttp.ClientSession() as session:
            post = await df.get_post_by_id(session, args.post_id, site)
            if not post:
                print(f"No post found with id {args.post_id}")
                return
            from startriage.output import OutputFormat
            from startriage.sources.discourse.triage import PostStatus, _print_single_comment

            _print_single_comment(
                post,
                PostStatus.UNCHANGED,
                post.get_update_time(),
                df.get_post_url_by_id(post, site),
                False,
                OutputFormat.TERMINAL,
                sys.stdout,
            )

    asyncio.run(_fetch())


def _run_set_defaults(args: argparse.Namespace, config_path) -> None:
    import tomllib

    import tomli_w

    from startriage.config import DEFAULT_USER_CONFIG

    path = (config_path or DEFAULT_USER_CONFIG).expanduser()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        data = {}

    team_section = data.setdefault("team", {}).setdefault(args.team, {})
    if args.site:
        data.setdefault("general", {})["discourse_site"] = args.site
    if args.category:
        team_section["discourse_categories"] = args.category
    if getattr(args, "default_team", None):
        data.setdefault("general", {})["default_team"] = args.default_team

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    print(f"Defaults saved to {path}")
