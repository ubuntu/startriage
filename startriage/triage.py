"""Generic entry point for all triage modes."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from .config import GeneralConfig, StarTriageConfig, TeamConfig
from .dates import compact_date_range, reverse_triage_task_day
from .enums import FetchMode, UpdateFilter
from .output import OutputConfig, OutputFormat, TriageOutput
from .savebugs import BugPersistor, SaveConfig
from .sources.discourse.triage import DiscourseTriage
from .sources.discourse.triage import find as discourse_find
from .sources.github.finder import get_github_token
from .sources.github.triage import GithubTriage
from .sources.github.triage import find as github_find
from .sources.launchpad.triage import LaunchpadTriage
from .sources.launchpad.triage import find as launchpad_find
from .spinner import Spinner

SOURCES_ALL = ("launchpad", "discourse", "github")


def resolve_sources(sources_arg: str | None) -> frozenset[str]:
    """Resolve a comma-separated --source string to canonical source names."""
    if not sources_arg:
        return frozenset(SOURCES_ALL)
    result = set()
    for raw in sources_arg.split(","):
        result.add(raw.strip().lower())
    return frozenset(result)


@dataclass
class TriageRunOptions:
    start: date | None
    end: date | None
    age: datetime
    old: datetime
    sources: frozenset[str]
    show_expiration: bool = True
    markdown_path: Path | None = None
    update_filter: UpdateFilter | None = None


async def run_triage(
    team_name: str,
    team_config: TeamConfig,
    general_config: GeneralConfig,
    opts: TriageRunOptions,
    output_cfg: OutputConfig,
) -> None:
    """Daily triage: fetch all sources concurrently, print sections in order as they complete."""

    github_token = get_github_token()

    async def _fetch_lp() -> LaunchpadTriage:
        return await launchpad_find(
            team_config,
            general_config,
            opts.start,
            opts.end,
            mode=FetchMode.triage,
            update_filter=opts.update_filter,
            age=opts.age,
            old=opts.old,
            show_expiration=opts.show_expiration,
        )

    async def _fetch_discourse() -> DiscourseTriage:
        discourse_start = (
            datetime.combine(opts.start, datetime.min.time()).replace(tzinfo=timezone.utc)
            if opts.start
            else None
        )
        discourse_end = (
            datetime.combine(opts.end, datetime.min.time()).replace(tzinfo=timezone.utc) if opts.end else None
        )

        if discourse_start:
            discourse_end_exclusive = discourse_end + timedelta(days=1) if discourse_end else None
        else:
            discourse_end_exclusive = None

        assert discourse_start is not None and discourse_end_exclusive is not None
        async with aiohttp.ClientSession() as session:
            return await discourse_find(
                session,
                team_config.discourse_categories,
                discourse_start,
                discourse_end_exclusive,
                triage_category_id=team_config.discourse_triage_category_id,
            )

    async def _fetch_github() -> GithubTriage:
        assert opts.start is not None and opts.end is not None
        return await github_find(
            team_config.github_repos,
            opts.start,
            opts.end,
            token=github_token,
            mode=FetchMode.triage,
        )

    # Schedule all three fetches concurrently
    fetch_tasks: dict[str, asyncio.Task[TriageOutput]] = {}
    if "launchpad" in opts.sources:
        fetch_tasks["launchpad"] = asyncio.create_task(_fetch_lp())
    if "discourse" in opts.sources:
        fetch_tasks["discourse"] = asyncio.create_task(_fetch_discourse())
    if "github" in opts.sources:
        fetch_tasks["github"] = asyncio.create_task(_fetch_github())

    # Print date range once before any section output
    if opts.start and opts.end:
        start = opts.start.strftime("%Y-%m-%d (%A)")
        end = opts.end.strftime("%Y-%m-%d (%A)")
        range = f" {compact_date_range(opts.start, opts.end)}"

        if opts.start == opts.end:
            range_verbose = f"on {start}"
        else:
            range_verbose = f"between {start} and {end} inclusive"

        triage_task_name = reverse_triage_task_day(opts.start, opts.end)

        if triage_task_name:
            triage_task_note = f' ("{triage_task_name}")'

    else:
        range = triage_task_note = ""

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"Triage{range} for team {team_name!r}", file=output_cfg.out)

    if range_verbose:
        match output_cfg.fmt:
            case OutputFormat.TERMINAL:
                print(f"Items updated {range_verbose}{triage_task_note}...", file=output_cfg.out)
            case OutputFormat.MARKDOWN:
                print(f"Items updated {range_verbose}\n", file=output_cfg.out)
            case _:
                raise NotImplementedError

    # Track which sources are still being fetched (shared mutable state, safe in single-threaded asyncio)
    pending = set(fetch_tasks.keys())

    # Print sections in canonical order as each completes
    async def _await_and_print(source: str, task: asyncio.Task, spinner: Spinner) -> tuple[str, TriageOutput]:
        result: TriageOutput = await task
        spinner.done(source)
        spinner.clear()
        spinner.suspend()  # prevent spinner redraws while section output is in progress
        try:
            await result.print_section(output_cfg)
            print(file=output_cfg.out)
        finally:
            spinner.resume()
        return source, result

    print(file=output_cfg.out)
    async with Spinner(pending) as spinner:
        results = await asyncio.gather(
            *[_await_and_print(source, t, spinner) for source, t in fetch_tasks.items()]
        )

    # create markdown template
    if opts.markdown_path:
        opts.markdown_path.write_text(f"# Triage{range}\n")

        # ensure the section order
        result_map = dict(results)
        for source in ("launchpad", "github", "discourse"):
            r = result_map[source]
            await r.write_markdown(opts.markdown_path)

        with opts.markdown_path.open("a", encoding="utf-8") as fh:
            fh.write("\n# Proposed Migration\n\n")
        logging.info("Markdown written to %s", opts.markdown_path)


async def run_todo(
    team_name: str,
    config: StarTriageConfig,
    opts: TriageRunOptions,
    output_cfg: OutputConfig,
    save_cfg: SaveConfig,
    subscribed: bool = False,
) -> None:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter.

    All sources in *opts.sources* are optional — pass a subset to fetch only
    that source.  *subscribed* only controls LP fetch mode (subscription list
    vs. todo tag); GitHub is filtered by label regardless.
    """
    mode = FetchMode.subscribed if subscribed else FetchMode.todo

    team_config = config.get_team(team_name)

    # GitHub label: explicit config field, fall back to lp_todo_tag in todo mode,
    # or no filter in subscribed mode
    gh_default_label: str | None = (
        None if subscribed else (team_config.github_todo_label or team_config.lp_todo_tag)
    )

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"bug housekeeping for team {team_name!r}\n")

    token = get_github_token()
    pending: set[str] = set()
    lp_task: asyncio.Task | None = None
    gh_task: asyncio.Task | None = None

    if "launchpad" in opts.sources:
        pending.add("launchpad")
        lp_task = asyncio.create_task(launchpad_find(team_config, config.general, None, None, mode=mode))

    if "github" in opts.sources:
        pending.add("github")
        gh_task = asyncio.create_task(
            github_find(
                team_config.github_repos,
                None,
                None,
                token=token,
                default_label=gh_default_label,
                mode=FetchMode.todo,
            )
        )

    handler = BugPersistor(save_cfg)

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

    if output_cfg.fmt == OutputFormat.JSON:
        result: dict = {}
        if lp_triage is not None:
            result["launchpad"] = json.loads(lp_triage.to_json())
        if gh_triage is not None:
            result["github"] = gh_triage.to_dict()
        print(json.dumps(result, indent=4, default=str), file=output_cfg.out)
        return

    if lp_triage is not None:
        await lp_triage.print_section(
            output_cfg,
            extended=True,
            bug_persistor=handler,
        )

    if gh_triage is not None:
        await gh_triage.print_section(
            output_cfg,
            bug_persistor=handler,
        )
