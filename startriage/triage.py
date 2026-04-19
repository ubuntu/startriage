"""Generic entry point for all triage modes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import aiohttp

from startriage.config import GeneralConfig, TeamConfig
from startriage.output import OutputFormat, print_section_header
from startriage.sources.discourse.triage import DiscourseTriage
from startriage.sources.discourse.triage import find as discourse_find
from startriage.sources.github.finder import find as github_find
from startriage.sources.github.finder import get_github_token
from startriage.sources.github.triage import GithubTriage
from startriage.sources.launchpad.finder import fetch_bugs
from startriage.sources.launchpad.triage import LaunchpadTriage

SOURCES_ALL = ("launchpad", "discourse", "github")
SOURCE_ALIASES = {
    "bugs": "launchpad",
    "forum": "discourse",
    "docs": "github",
    "documentation": "github",
}


def resolve_sources(sources_arg: str | None) -> frozenset[str]:
    """Resolve a comma-separated --source string to canonical source names."""
    if not sources_arg:
        return frozenset(SOURCES_ALL)
    result = set()
    for raw in sources_arg.split(","):
        canonical = raw.strip().lower()
        result.add(SOURCE_ALIASES.get(canonical, canonical))
    return frozenset(result)


@dataclass
class TriageRunOptions:
    start: date | None
    end: date | None
    sources: frozenset[str]
    open_in_browser: bool = False
    shorten_links: bool = True
    show_expiration: bool = True
    fmt: OutputFormat = OutputFormat.TERMINAL
    markdown_path: str | None = None
    update_filter: Literal["theirs", "ours", "all"] | None = (
        None  # overrides general_config.lp_update_filter if set
    )


async def run_triage(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    opts: TriageRunOptions,
) -> None:
    """Daily triage: fetch all sources concurrently, print sections in order as they complete."""

    token = get_github_token()
    discourse_start = (
        datetime.combine(opts.start, datetime.min.time()).replace(tzinfo=timezone.utc) if opts.start else None
    )
    discourse_end = (
        datetime.combine(opts.end, datetime.min.time()).replace(tzinfo=timezone.utc) if opts.end else None
    )

    if discourse_start:
        discourse_end_exclusive = discourse_end + timedelta(days=1) if discourse_end else None
    else:
        discourse_end_exclusive = None

    async def _fetch_lp():
        if opts.update_filter:
            general_config.lp_update_filter = opts.update_filter
        return await fetch_bugs(team_config, general_config, opts.start, opts.end, mode="triage")

    async def _fetch_discourse():
        assert discourse_start is not None and discourse_end_exclusive is not None
        async with aiohttp.ClientSession() as session:
            return await discourse_find(
                session,
                team_config.discourse_categories,
                discourse_start,
                discourse_end_exclusive,
                site=general_config.discourse_site,
            )

    async def _fetch_github():
        assert opts.start is not None and opts.end is not None
        return await github_find(
            team_config.github_org,
            team_config.github_repos,
            opts.start,
            opts.end,
            token=token,
        )

    # Schedule all three fetches concurrently
    fetch_tasks = {}
    if "launchpad" in opts.sources:
        fetch_tasks["launchpad"] = asyncio.create_task(_fetch_lp())
    if "discourse" in opts.sources:
        fetch_tasks["discourse"] = asyncio.create_task(_fetch_discourse())
    if "github" in opts.sources:
        fetch_tasks["github"] = asyncio.create_task(_fetch_github())

    # Print sections in canonical order as each completes
    async def _await_and_print(key: str, task: asyncio.Task, print_fn):
        result = await task
        print_fn(result)
        return key, result

    results = await asyncio.gather(
        *[
            _await_and_print(key, t, lambda r, k=key: _print_result(k, r, opts))
            for key, t in fetch_tasks.items()
        ]
    )

    # Write markdown output if requested
    if opts.markdown_path:
        # Truncate/create file first
        Path(opts.markdown_path).write_text("")
        result_map = dict(results)
        for key in ("launchpad", "github", "discourse"):
            r = result_map.get(key)
            if r:
                r.write_markdown(opts.markdown_path)
        with open(opts.markdown_path, "a", encoding="utf-8") as fh:
            fh.write("\n# Proposed Migration\n\n")
        logging.info("Markdown written to %s", opts.markdown_path)

    # Always print Proposed Migration header to terminal
    print_section_header("Proposed Migration")


def _print_result(source: str, result, opts: TriageRunOptions) -> None:
    if source == "launchpad" and isinstance(result, LaunchpadTriage):
        result.print_section(fmt=opts.fmt, open_in_browser=opts.open_in_browser)
    elif source == "github" and isinstance(result, GithubTriage):
        result.print_section(fmt=opts.fmt, open_in_browser=opts.open_in_browser)
    elif source == "discourse" and isinstance(result, DiscourseTriage):
        result.print_section(
            fmt=opts.fmt, open_in_browser=opts.open_in_browser, shorten_links=opts.shorten_links
        )


async def run_todo(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    opts: TriageRunOptions,
    filename_save: str | None = None,
    filename_compare: str | None = None,
    filename_postponed: str | None = None,
    no_save: bool = False,
    limit: int | None = None,
    subscribed: bool = False,
    json_output: bool = False,
) -> None:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter."""

    mode = "subscribed" if subscribed else "todo"
    lp_triage = await fetch_bugs(team_config, general_config, None, None, mode=mode)

    if json_output:
        print(lp_triage.to_json())
        return

    # Auto-derive savebugs paths
    savebugs_dir = general_config.savebugs_dir
    if not no_save:
        auto_save = savebugs_dir / f"todo-{datetime.now().strftime('%Y-%m-%d')}.yaml"
        save_path = filename_save or str(auto_save)

        if filename_compare is None:
            existing = sorted(savebugs_dir.glob("todo-*.yaml"))
            compare_path = str(existing[-1]) if existing else None
        else:
            compare_path = filename_compare

        auto_postponed = savebugs_dir / "postponed.yaml"
        postponed_path = filename_postponed or (str(auto_postponed) if auto_postponed.exists() else None)
    else:
        save_path = compare_path = postponed_path = None

    if save_path and not no_save:
        logging.info("Will save bug list to: %s", save_path)

    lp_triage.print_section(
        fmt=opts.fmt,
        open_in_browser=opts.open_in_browser,
        filename_save=save_path if not no_save else None,
        filename_compare=compare_path,
        filename_postponed=postponed_path,
        limit=limit,
    )

    # GitHub: tagged issues in configured repos
    if not subscribed and "github" in opts.sources:
        token = get_github_token()
        gh_triage = await github_find(
            team_config.github_org,
            team_config.github_repos,
            None,
            None,
            token=token,
        )
        gh_triage.print_section(fmt=opts.fmt)
