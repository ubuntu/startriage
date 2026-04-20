"""CLI argument parsing for startriage."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import tomli_w

from startriage.config import DEFAULT_USER_CONFIG, StarTriageConfig, load_config
from startriage.dates import parse_interval
from startriage.enums import UpdateFilter
from startriage.log import log_setup
from startriage.output import OutputFormat
from startriage.sources.discourse import finder as discourse_finder
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
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase logging verbosity (repeatable)"
    )
    parser.add_argument("-q", "--quiet", action="count", default=0, help="Reduce logging verbosity")
    parser.add_argument(
        "-o", "--open", action="store_true", dest="open_in_browser", help="Open items in web browser"
    )
    parser.add_argument("--fullurls", action="store_true", help="Show full URLs instead of hyperlinks")

    # Shared parent parser for subcommands that support --markdown output
    markdown_p = argparse.ArgumentParser(add_help=False)
    markdown_p.add_argument(
        "--markdown",
        metavar="PATH",
        help="Write parallel markdown output to PATH (for Discourse post template)",
    )

    sp = parser.add_subparsers(required=True, metavar="COMMAND")

    # --- list ---
    list_p = sp.add_parser("list", help="List triage items")
    list_sp = list_p.add_subparsers(required=True, metavar="SUBCOMMAND")

    triage_p = list_sp.add_parser("triage", help="Daily triage", parents=[markdown_p])
    _add_triage_args(triage_p)
    triage_p.set_defaults(func=_run_triage)

    todo_p = list_sp.add_parser("todo", help="Tagged bug housekeeping", parents=[markdown_p])
    _add_todo_args(todo_p)
    todo_p.set_defaults(func=_run_todo)

    # --- forum ---
    forum_p = sp.add_parser("forum", help="Discourse forum commands")
    forum_sp = forum_p.add_subparsers(required=True, metavar="SUBCOMMAND")

    forum_backlog_p = forum_sp.add_parser(
        "backlog", help="Print a single post in backlog format", parents=[markdown_p]
    )
    forum_backlog_p.add_argument("post_id", type=int, help="Discourse post ID")
    forum_backlog_p.add_argument("-s", "--site", help="Discourse site URL")
    forum_backlog_p.set_defaults(func=_run_backlog)

    # --- config ---
    config_p = sp.add_parser("config", help="Manage configuration")
    config_sp = config_p.add_subparsers(required=True)

    config_setdefaults_p = config_sp.add_parser("set", help="Persist settings to config file")
    config_setdefaults_p.add_argument("--discourse-site", help="Discourse website base URL")
    config_setdefaults_p.add_argument("--discourse-category", help="Discourse category")
    config_setdefaults_p.add_argument("--default-team", help="Set general.default_team in config")
    config_setdefaults_p.set_defaults(func=_set_config_settings)

    return parser


def _add_triage_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-i",
        "--interval",
        default=None,
        metavar="DATE[:DATE]",
        help=("Date interval to select only tasks changed on that day/inside the range: "
              "YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, or day name (e.g. monday)"),
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
    p.add_argument("--flag-recent", type=int, metavar="DAYS", default=None)
    p.add_argument("--flag-old", type=int, metavar="DAYS", default=None)
    p.add_argument("--no-ignore-list", action="store_true", help="Include normally-ignored packages")
    p.add_argument(
        "--update",
        choices=UpdateFilter,
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


def _make_opts(args: argparse.Namespace) -> TriageRunOptions:
    start, end = parse_interval(args.interval)
    age = (
        datetime.now(timezone.utc) - timedelta(days=args.flag_recent)
        if args.flag_recent is not None
        else None
    )
    old = datetime.now(timezone.utc) - timedelta(days=args.flag_old) if args.flag_old is not None else None
    return TriageRunOptions(
        start=start,
        end=end,
        sources=resolve_sources(args.source),
        open_in_browser=args.open_in_browser,
        shorten_links=not args.fullurls,
        fmt=OutputFormat.TERMINAL,
        markdown_path=Path(args.markdown) if args.markdown else None,
        update_filter=args.update,
        age=age,
        old=old,
    )


def main() -> None:
    asyncio.run(_run())


async def _run() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    log_setup(args.verbose - args.quiet)

    config = load_config(args.config)

    await args.func(args, config)


async def _run_triage(args: argparse.Namespace, config: StarTriageConfig) -> None:
    team = config.get_team(_resolve_team_name(args.team, config))
    if args.no_ignore_list:
        team = team.model_copy(update={"lp_ignore_packages": []})
    await run_triage(team, config.general, _make_opts(args))


async def _run_todo(args: argparse.Namespace, config: StarTriageConfig) -> None:
    team = config.get_team(_resolve_team_name(args.team, config))
    if args.flag_recent is None and not args.subscribed:
        args.flag_recent = 6  # default flag-recent for todo mode
    opts = dataclasses.replace(
        _make_opts(args),
        sources=frozenset(["launchpad", "github"]),
    )
    await run_todo(
        team,
        config.general,
        opts,
        filename_save=args.save,
        filename_compare=args.compare,
        filename_postponed=args.postponed,
        no_save=args.no_save,
        limit=args.limit,
        subscribed=args.subscribed,
        json_output=args.json_output,
    )


async def _run_backlog(args: argparse.Namespace, config: StarTriageConfig) -> None:
    site = args.discourse_site or config.general.discourse_site

    async with aiohttp.ClientSession() as session:
        post = await discourse_finder.get_post_by_id(session, args.post_id, site)
        if not post:
            print(f"No post found with id {args.post_id}")
            return
        from startriage.sources.discourse.triage import PostStatus, _print_single_comment

        _print_single_comment(
            post,
            PostStatus.UNCHANGED,
            post.get_update_time(),
            discourse_finder.get_post_url_by_id(post, site),
            False,
            OutputFormat.TERMINAL,
            sys.stdout,
        )


async def _set_config_settings(args: argparse.Namespace, _config: StarTriageConfig) -> None:
    path = (args.config or DEFAULT_USER_CONFIG).expanduser()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        logging.debug("user config file not found at %s, using defaults.", path)
        data = {}

    if args.default_team:
        data.setdefault("general", {})["default_team"] = args.default_team
    if args.discourse_site:
        data.setdefault("general", {})["discourse_site"] = args.discourse_site
    if args.discourse_category:
        if not args.team:
            raise ValueError("error: --discourse-category requires -t/--team")
        team_section = data.setdefault("team", {}).setdefault(args.team, {})
        team_section["discourse_categories"] = args.discourse_category

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    print(f"Settings saved to {path!r}")
