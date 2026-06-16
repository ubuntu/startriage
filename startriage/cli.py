"""CLI argument parsing for startriage."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_USER_CONFIG, StarTriageConfig, load_config, resolve_team_name, update_user_config
from .dates import parse_interval, triage_task_date_range
from .enums import AIProvider, UpdateFilter
from .log import log_setup
from .output import OutputConfig, OutputFormat
from .savebugs import BugPersistor, SaveConfig
from .source import TaskFilterOptions
from .sources.github.auth import _run_github_login
from .triage import SOURCES, resolve_sources, run_todo, run_triage


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
        help=f"Path to config TOML (default: {DEFAULT_USER_CONFIG})",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase logging verbosity (repeatable)"
    )
    parser.add_argument("-q", "--quiet", action="count", default=0, help="Reduce logging verbosity")

    # Shared parent parser for subcommands that support --markdown output
    output_p = argparse.ArgumentParser(add_help=False)
    output_p.add_argument(
        "--markdown",
        metavar="PATH",
        help="Write parallel markdown output to PATH (for Discourse post template)",
    )
    output_p.add_argument(
        "--format",
        choices=OutputFormat,
        default=OutputFormat.TERMINAL,
        help="Output format (default: %(default)s)",
    )
    output_p.add_argument(
        "-o", "--open", action="store_true", dest="open_in_browser", help="Open items in web browser"
    )
    output_p.add_argument("--fullurls", action="store_true", help="Show full URLs instead of hyperlinks")

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
        "-d",
        "--triage-day",
        default=None,
        metavar="DAY",
        help=("Triage task day to deduce interval from. 'monday' -> fri,sa,sun. tuesday -> mon."),
    )

    taskfilter_p.add_argument(
        "-s",
        "--source",
        default=None,
        metavar="SOURCE[,SOURCE]",
        help=f"Comma-separated sources to include: {', '.join(SOURCES.keys())} (default: all)",
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
  X  expiring (not seen in today's window, --expire-level1/2 days)
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
        parents=[output_p, taskfilter_p, list_p],
    )
    triage_p.add_argument("--no-expiration", action="store_true", help="Skip expiring bugs subsection")
    triage_p.add_argument(
        "--expire-level1",
        type=int,
        metavar="DAYS",
        help="Days to re-display old expiring bugs (level 1)",
    )
    triage_p.add_argument(
        "--expire-level2",
        type=int,
        metavar="DAYS",
        help="Days to re-display old expiring bugs (level 2)",
    )
    triage_p.add_argument(
        "--extended",
        type=_bool_flag,
        metavar="BOOL",
        help="Display more bug information (assignee). default: %(default)s",
    )
    triage_p.add_argument(
        "--update",
        choices=UpdateFilter,
        help="Filter by who last updated bugs (default: theirs)",
    )
    triage_p.add_argument(
        "--proposed-min-age",
        type=int,
        metavar="DAYS",
        help="Minimum days of being stuck in proposed to be included in triage",
    )
    triage_p.set_defaults(func=_run_triage)

    # --- todo ---
    todo_p = sp.add_parser("todo", help="Tagged bug housekeeping", parents=[output_p, taskfilter_p, list_p])
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
    config_setdefaults_p.add_argument("--discourse-categories", help="Discourse category (comma separated)")
    config_setdefaults_p.add_argument("--default-team", help="Set general.default_team in config")
    config_setdefaults_p.add_argument(
        "--save-bugs-dir", metavar="PATH", help="Directory to track previous bugs in"
    )
    config_setdefaults_p.add_argument(
        "--proposed-min-age",
        type=int,
        metavar="DAYS",
        help="Set days of being stuck in proposed (config's general.proposed_min_age)",
    )
    config_setdefaults_p.add_argument(
        "--github-token",
        metavar="TOKEN",
        help=(
            "Set GitHub API token in config (use 'gh' to auto-fetch from GitHub CLI). "
            "Alternatively set the GITHUB_TOKEN environment variable."
        ),
    )
    config_setdefaults_p.add_argument(
        "--ai-provider",
        choices=AIProvider,
        help="Set AI triage provider in config (ai.provider)",
    )
    config_setdefaults_p.add_argument(
        "--ai-model",
        metavar="MODEL",
        help="Set AI triage model in config (ai.model)",
    )
    config_setdefaults_p.add_argument(
        "--ai-github-token",
        metavar="TOKEN",
        help=(
            "Set Copilot GitHub token in config (ai.github_token). "
            "Alternatively set the COPILOT_GITHUB_TOKEN environment variable."
        ),
    )
    config_setdefaults_p.add_argument(
        "--ai-openrouter-key",
        metavar="KEY",
        help=(
            "Set OpenRouter API key in config (ai.openrouter_api_key). "
            "Alternatively set the OPENROUTER_API_KEY environment variable."
        ),
    )
    config_setdefaults_p.add_argument(
        "--ai-openrouter-base-url",
        metavar="URL",
        help="Set OpenRouter base URL in config (ai.openrouter_base_url)",
    )
    config_setdefaults_p.set_defaults(func=_set_config_settings)

    config_show_p = config_sp.add_parser("show", help="Display resolved configuration")
    config_show_p.set_defaults(func=_show_config)

    # --- github ---
    github_p = sp.add_parser("github", help="GitHub integration commands")
    github_sp = github_p.add_subparsers(required=True)

    github_login_p = github_sp.add_parser("login", help="Authenticate with GitHub via device flow")
    github_login_p.set_defaults(func=_run_github_login)

    return parser


def _bool_flag(value: str) -> bool:
    """
    parse a boolean flag from argument.
    we do this to also allow None (= unset, which doesn't work with action=store_true).
    """
    match value.lower():
        case "true" | "1" | "yes" | "y":
            return True
        case "false" | "0" | "no" | "n":
            return False
        case _:
            raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}")


def _filter_from_args(
    config: StarTriageConfig, args: argparse.Namespace, source_filter: set[str] | None = None
) -> TaskFilterOptions:
    # mutually exclusive options in parser
    if args.interval:
        start, end = parse_interval(args.interval)
    else:
        start, end = triage_task_date_range(args.triage_day)

    recent_since: datetime = datetime.now(timezone.utc) - timedelta(days=args.flag_recent)
    old_since: datetime = datetime.now(timezone.utc) - timedelta(days=args.flag_old)
    team_name = resolve_team_name(args.team, config)

    update_filter = getattr(args, "update", None)  # only for triage command

    return TaskFilterOptions(
        team=team_name,
        start=start,
        end=end,
        recent_since=recent_since,
        old_since=old_since,
        sources=resolve_sources(args.source, source_filter),
        show_expiration=not getattr(args, "no_expiration", False),
        update_filter=update_filter,
    )


def _outputcfg_from_args(args: argparse.Namespace, persistor: BugPersistor | None = None) -> OutputConfig:
    return OutputConfig(
        fmt=args.format,
        out=sys.stdout,
        open_in_browser=args.open_in_browser,
        terminal_links=not args.fullurls,
        markdown_path=Path(args.markdown) if args.markdown else None,
        bug_persistor=persistor,
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
    filter = _filter_from_args(config, args)
    team = config.get_team(filter.team)
    if args.no_ignore_list:
        team = team.model_copy(update={"lp_ignore_packages": []})

    general = config.general
    if args.expire_level1 is not None:
        general = general.model_copy(update={"lp_expire_level1_days": args.expire_level1})
    if args.expire_level2 is not None:
        general = general.model_copy(update={"lp_expire_level2_days": args.expire_level2})
    if args.extended is not None:
        general = general.model_copy(update={"lp_extended": args.extended})
    if args.proposed_min_age is not None:
        general = general.model_copy(update={"proposed_min_age": args.proposed_min_age})
    config.general = general

    output_cfg = _outputcfg_from_args(args)
    await run_triage(config, filter, output_cfg)


async def _run_todo(args: argparse.Namespace, config: StarTriageConfig) -> None:
    if args.flag_recent is None and not args.subscribed:
        args.flag_recent = 6  # default flag-recent for todo mode

    filter = _filter_from_args(config, args, source_filter={"launchpad", "github"})

    save_cfg = SaveConfig(
        savebugs_dir=Path(args.save_bugs_dir) if args.save_bugs_dir else config.general.savebugs_dir,
        override_save=Path(args.save) if args.save else None,
        override_compare=Path(args.compare) if args.compare else None,
        no_save=args.no_save,
    )

    output_cfg = _outputcfg_from_args(args, BugPersistor(save_cfg))

    await run_todo(
        config,
        filter,
        output_cfg=output_cfg,
        subscribed=args.subscribed,
    )


async def _set_config_settings(args: argparse.Namespace, _config: StarTriageConfig) -> None:
    updates: dict[str, dict] = {}

    if args.default_team:
        updates.setdefault("general", {})["default_team"] = args.default_team
    if args.discourse_site:
        updates.setdefault("general", {})["discourse_site"] = args.discourse_site
    if args.discourse_categories:
        if not args.team:
            raise ValueError("error: --discourse-categories requires -t/--team")
        updates.setdefault("team", {}).setdefault(args.team, {})["discourse_categories"] = (
            args.discourse_categories.split(",")
        )
    if args.save_bugs_dir:
        if not Path(args.save_bugs_dir).is_dir():
            raise ValueError(f"error: --save-bugs-dir {args.save_bugs_dir!r} is not a directory")
        updates.setdefault("general", {})["savebugs_dir"] = args.save_bugs_dir
    if args.proposed_min_age is not None:
        updates.setdefault("general", {})["proposed_min_age"] = args.proposed_min_age
    if args.github_token is not None:
        updates.setdefault("general", {})["github_token"] = args.github_token
    if args.ai_provider:
        updates.setdefault("ai", {})["provider"] = str(args.ai_provider)
    if args.ai_model:
        updates.setdefault("ai", {})["model"] = args.ai_model
    if args.ai_github_token is not None:
        updates.setdefault("ai", {})["github_token"] = args.ai_github_token
    if args.ai_openrouter_key is not None:
        updates.setdefault("ai", {})["openrouter_api_key"] = args.ai_openrouter_key
    if args.ai_openrouter_base_url:
        updates.setdefault("ai", {})["openrouter_base_url"] = args.ai_openrouter_base_url

    if not updates:
        print("No settings to update.")
        return

    sensitive = "github_token" in updates.get("general", {}) or bool(
        {"github_token", "openrouter_api_key"} & updates.get("ai", {}).keys()
    )
    path = update_user_config(updates, config_path=args.config, sensitive=sensitive)
    print(f"Settings saved to {path!r}")


async def _show_config(args: argparse.Namespace, config: StarTriageConfig) -> None:
    print(config.show())
