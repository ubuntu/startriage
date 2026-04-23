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

import tomli_w

from startriage.config import DEFAULT_USER_CONFIG, StarTriageConfig, load_config, resolve_team_name
from startriage.dates import parse_interval, triage_task_date_range
from startriage.enums import UpdateFilter
from startriage.log import log_setup
from startriage.output import OutputConfig, OutputFormat
from startriage.savebugs import SaveConfig
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

    taskfilter_p = argparse.ArgumentParser(add_help=False)
    interval_exclusive_group = taskfilter_p.add_mutually_exclusive_group()

    interval_exclusive_group.add_argument(
        "-i",
        "--interval",
        default=None,
        metavar="DATE[:DATE]",
        help=(
            "Date interval to select only tasks changed on that day/inside the range: "
            "YYYY-MM-DD, YYYY-MM-DD:YYYY-MM-DD, "
            "or a relative date ('yesterday'). make open ended by ':', e.g. 'yesterday:'."
        ),
    )

    interval_exclusive_group.add_argument(
        "-t",
        "--triage-day",
        default=None,
        metavar="DAY",
        help=("Triage task day to deduce interval from. 'monday' -> fri,sa,sun. tuesday -> mon."),
    )

    taskfilter_p.add_argument(
        "--source",
        default=None,
        metavar="SOURCE[,SOURCE]",
        help="Comma-separated sources to include: launchpad, discourse, github",
    )
    taskfilter_p.add_argument(
        "--flag-recent",
        type=int,
        default=7,
        metavar="DAYS",
        help="Mark bugs updated within N days with `U` flag (default: %(default)s)",
    )
    taskfilter_p.add_argument(
        "--flag-old",
        type=int,
        default=30,
        metavar="DAYS",
        help="Mark bugs inactive for more than N days with `O` flag (default: %(default)s)",
    )
    taskfilter_p.add_argument("--no-ignore-list", action="store_true", help="Include ignored ubuntu packages")

    list_p = argparse.ArgumentParser(
        add_help=False,
        epilog="""\
Terminal output — bug flags column (left to right):
  *  subscribed by the team
  +  last activity NOT from the team (reply pending)
  U  updated recently (within --flag-recent days)
  O  old / dormant (beyond --flag-old days)
  X  expiring (not seen in today's window, --expire-tagged/--expire days)
  N  new bug since last --compare file
  v  verification-needed-* tag set
  V  verification-done-* tag set

Colors:
RED = needs attention
BLUE = waiting in unapproved queue
GREEN = done
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sp = parser.add_subparsers(required=True, metavar="COMMAND")

    # --- triage ---
    triage_p = sp.add_parser(
        "triage",
        help="Daily triage",
        parents=[markdown_p, taskfilter_p, list_p],
    )
    triage_p.add_argument("--no-expiration", action="store_true", help="Skip expiring bugs subsection")
    triage_p.add_argument(
        "--expire-tagged",
        type=int,
        metavar="DAYS",
        help="Days to consider todo-tagged bugs expired if no update happened",
    )
    triage_p.add_argument(
        "--expire",
        type=int,
        metavar="DAYS",
        help="Days to consider subscribed bugs expired if no update happened",
    )
    triage_p.add_argument(
        "--update",
        choices=UpdateFilter,
        help="Filter by who last updated bugs (default: theirs)",
    )
    triage_p.set_defaults(func=_run_triage)

    # --- todo ---
    todo_p = sp.add_parser("todo", help="Tagged bug housekeeping", parents=[markdown_p, taskfilter_p, list_p])
    todo_p.add_argument(
        "--subscribed",
        action="store_true",
        help="Show subscription backlog (directly subscribed, tag excluded)",
    )

    todo_p.add_argument("--save-bugs-dir", metavar="PATH", help="Directory to track previous bugs in")
    todo_p.add_argument("-S", "--save", metavar="PATH", help="Set filename to save bugs in")
    todo_p.add_argument("--no-save", action="store_true", help="Do not actually save bug list to file")
    todo_p.add_argument("-C", "--compare", metavar="PATH", help="Set path to saved file to compare bugs to")
    todo_p.set_defaults(func=_run_todo)

    # --- config ---
    config_p = sp.add_parser("config", help="Manage configuration")
    config_sp = config_p.add_subparsers(required=True)

    config_setdefaults_p = config_sp.add_parser("set", help="Persist settings to config file")
    config_setdefaults_p.add_argument("--discourse-site", help="Discourse website base URL")
    config_setdefaults_p.add_argument("--discourse-category", help="Discourse category")
    config_setdefaults_p.add_argument("--default-team", help="Set general.default_team in config")
    config_setdefaults_p.set_defaults(func=_set_config_settings)

    config_show_p = config_sp.add_parser("show", help="Display resolved configuration")
    config_show_p.set_defaults(func=_show_config)

    return parser


def _make_opts(args: argparse.Namespace) -> TriageRunOptions:
    # mutually exclusive options in parser
    if args.triage_day:
        start, end = triage_task_date_range(args.triage_day)
    else:
        start, end = parse_interval(args.interval)

    age = datetime.now(timezone.utc) - timedelta(days=args.flag_recent)
    old = datetime.now(timezone.utc) - timedelta(days=args.flag_old)
    return TriageRunOptions(
        start=start,
        end=end,
        sources=resolve_sources(args.source),
        show_expiration=not getattr(args, "no_expiration", False),
        markdown_path=Path(args.markdown) if args.markdown else None,
        update_filter=getattr(args, "update", None),
        age=age,
        old=old,
    )


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(130)


async def _run() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    log_setup(args.verbose - args.quiet)

    config = load_config(args.config)

    await args.func(args, config)


async def _run_triage(args: argparse.Namespace, config: StarTriageConfig) -> None:
    team_name = resolve_team_name(args.team, config)
    team = config.get_team(team_name)
    if args.no_ignore_list:
        team = team.model_copy(update={"lp_ignore_packages": []})

    general = config.general
    if args.expire_tagged is not None:
        general = general.model_copy(update={"lp_expire_tagged": args.expire_tagged})
    if args.expire is not None:
        general = general.model_copy(update={"lp_expire": args.expire})

    output_cfg = OutputConfig(
        fmt=OutputFormat.TERMINAL,
        out=sys.stdout,
        open_in_browser=args.open_in_browser,
        terminal_links=not args.fullurls,
    )
    await run_triage(team_name, team, general, _make_opts(args), output_cfg=output_cfg)


async def _run_todo(args: argparse.Namespace, config: StarTriageConfig) -> None:
    if args.flag_recent is None and not args.subscribed:
        args.flag_recent = 6  # default flag-recent for todo mode
    opts = dataclasses.replace(
        _make_opts(args),
        sources=frozenset(["launchpad", "github"]),
    )

    output_cfg = OutputConfig(
        fmt=OutputFormat.TERMINAL,
        out=sys.stdout,
        open_in_browser=args.open_in_browser,
        terminal_links=not args.fullurls,
    )

    save_cfg = SaveConfig(
        savebugs_dir=Path(args.save_bugs_dir) if args.save_bugs_dir else config.general.savebugs_dir,
        override_save=Path(args.save) if args.save else None,
        override_compare=Path(args.compare) if args.compare else None,
        no_save=args.no_save,
    )

    await run_todo(
        args.team,
        config,
        opts,
        output_cfg=output_cfg,
        save_cfg=save_cfg,
        subscribed=args.subscribed,
    )


async def _show_config(args: argparse.Namespace, config: StarTriageConfig) -> None:
    print("[general]")
    for field, value in config.general.model_dump().items():
        print(f"  {field} = {value!r}")
    for team_name, team in config.team.items():
        print()
        print(f"[team.{team_name}]")
        for field, value in team.model_dump().items():
            print(f"  {field} = {value!r}")


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
