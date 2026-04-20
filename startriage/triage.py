"""Generic entry point for all triage modes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from .config import GeneralConfig, TeamConfig
from .dates import reverse_auto_date_range
from .enums import FetchMode, UpdateFilter
from .output import OutputFormat
from .savebugs import get_bug_persistor
from .sources.discourse.triage import find as discourse_find
from .sources.github.finder import get_github_token
from .sources.github.triage import find as github_find
from .sources.launchpad.triage import find as launchpad_find
from .spinner import Spinner

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
    markdown_path: Path | None = None
    update_filter: UpdateFilter | None = None
    age: datetime | None = None
    old: datetime | None = None


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
        return await launchpad_find(
            team_config,
            general_config,
            opts.start,
            opts.end,
            mode=FetchMode.triage,
            update_filter=opts.update_filter,
            age=opts.age,
            old=opts.old,
        )

    async def _fetch_discourse():
        assert discourse_start is not None and discourse_end_exclusive is not None
        async with aiohttp.ClientSession() as session:
            return await discourse_find(
                session,
                team_config.discourse_categories,
                discourse_start,
                discourse_end_exclusive,
                site=general_config.discourse_site,
                triage_category_id=team_config.discourse_triage_category_id,
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

    # Print date range once before any section output
    if opts.start and opts.end:
        pretty_start = opts.start.strftime("%Y-%m-%d (%A)")
        pretty_end = opts.end.strftime("%Y-%m-%d (%A)")
        if opts.start == opts.end:
            print(f"Updated on {pretty_start}")
        else:
            print(f"Updated between {pretty_start} and {pretty_end} inclusive")
        label = reverse_auto_date_range(opts.start, opts.end)
        if label:
            print(f"({label})")
        print()

    # Track which sources are still being fetched (shared mutable state, safe in single-threaded asyncio)
    pending = set(fetch_tasks.keys())

    # Print sections in canonical order as each completes
    async def _await_and_print(source: str, task: asyncio.Task):
        result = await task
        spinner.done(source)
        spinner.clear()
        spinner.suspend()  # prevent spinner redraws while section output is in progress
        try:
            match source:
                case "launchpad":
                    await result.print_section(fmt=opts.fmt, open_in_browser=opts.open_in_browser)
                case "github":
                    await result.print_section(fmt=opts.fmt, open_in_browser=opts.open_in_browser)
                case "discourse":
                    await result.print_section(
                        fmt=opts.fmt, open_in_browser=opts.open_in_browser, shorten_links=opts.shorten_links
                    )
                case _:
                    raise RuntimeError(f"Unhandled source {source!r}")
        finally:
            spinner.resume()
        return source, result

    async with Spinner(pending) as spinner:
        results = await asyncio.gather(*[_await_and_print(source, t) for source, t in fetch_tasks.items()])

    # create markdown template
    if opts.markdown_path:
        # Truncate/create file first
        opts.markdown_path.write_text("")
        result_map = dict(results)
        for source in ("launchpad", "github", "discourse"):
            r = result_map[source]
            await r.write_markdown(opts.markdown_path)

        with opts.markdown_path.open("a", encoding="utf-8") as fh:
            fh.write("\n# Proposed Migration\n\n")
        logging.info("Markdown written to %s", opts.markdown_path)


async def run_todo(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    opts: TriageRunOptions,
    filename_save: Path | None = None,
    filename_compare: Path | None = None,
    filename_postponed: Path | None = None,
    no_save: bool = False,
    limit: int | None = None,
    subscribed: bool = False,
    json_output: bool = False,
) -> None:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter.

    All sources in *opts.sources* are optional — pass a subset to fetch only
    that source.  *subscribed* only controls LP fetch mode (subscription list
    vs. todo tag); GitHub is filtered by label regardless.
    """
    mode = FetchMode.subscribed if subscribed else FetchMode.todo

    # GitHub label: explicit config field, fall back to lp_todo_tag in todo mode,
    # or no filter in subscribed mode
    gh_label: str | None = None if subscribed else (team_config.github_todo_label or team_config.lp_todo_tag)

    token = get_github_token()
    pending: set[str] = set()
    lp_task: asyncio.Task | None = None
    gh_task: asyncio.Task | None = None

    if "launchpad" in opts.sources:
        pending.add("launchpad")
        lp_task = asyncio.create_task(launchpad_find(team_config, general_config, None, None, mode=mode))
    if "github" in opts.sources:
        pending.add("github")
        gh_task = asyncio.create_task(
            github_find(
                team_config.github_org,
                team_config.github_repos,
                None,
                None,
                token=token,
                label=gh_label,
            )
        )

    handler = get_bug_persistor(
        general_config.savebugs_dir,
        filename_save=filename_save,
        filename_compare=filename_compare,
        filename_postponed=filename_postponed,
        no_save=no_save,
    )

    async with Spinner(pending) as spinner:
        lp_triage = None
        if lp_task is not None:
            lp_triage = await lp_task
            spinner.done("launchpad")
            spinner.clear()

        gh_triage = None
        if gh_task is not None:
            gh_triage = await gh_task
            spinner.done("github")
            spinner.clear()

    if json_output:
        result: dict = {}
        if lp_triage is not None:
            result["launchpad"] = json.loads(lp_triage.to_json())
        if gh_triage is not None:
            result["github"] = gh_triage.to_dict()
        print(json.dumps(result, indent=4, default=str))
        return

    if lp_triage is not None:
        await lp_triage.print_section(
            fmt=opts.fmt,
            open_in_browser=opts.open_in_browser,
            extended=True,
            bug_persistor=handler,
            limit=limit,
        )

    if gh_triage is not None:
        await gh_triage.print_todo_section(
            fmt=opts.fmt,
            open_in_browser=opts.open_in_browser,
            bug_persistor=handler,
        )
