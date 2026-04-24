"""Generic entry point for all triage modes."""

from __future__ import annotations

import asyncio
import logging
from datetime import time as _time

from .config import StarTriageConfig
from .dates import compact_date_range, reverse_triage_task_day
from .enums import FetchMode
from .output import OutputConfig, OutputFormat, TriageResult
from .source import TaskFilterOptions, TriageSource
from .sources.discourse.triage import find as discourse_find
from .sources.github.triage import find as github_find
from .sources.launchpad.triage import find as launchpad_find
from .sources.proposed.triage import find as proposed_find
from .spinner import Spinner

SOURCES = {
    "launchpad": TriageSource(name="launchpad", find=launchpad_find),
    "discourse": TriageSource(name="discourse", find=discourse_find),
    "github": TriageSource(name="github", find=github_find),
    "proposed": TriageSource(name="proposed", find=proposed_find),
}


def resolve_sources(
    sources_arg: str | None, source_filter: set[str] | None = None
) -> frozenset[TriageSource]:
    """Resolve a comma-separated --source string to canonical source names."""
    if not sources_arg:
        result = set(SOURCES.values())
    else:
        result = set()
        for raw in sources_arg.split(","):
            key = raw.strip().lower()
            if key in SOURCES:
                result.add(SOURCES[key])
    if source_filter is not None:
        result = {s for s in result if s.name in source_filter}
    return frozenset(result)


async def run_triage(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
) -> None:
    """Daily triage: fetch all sources concurrently, print sections in order as they complete."""

    range = triage_task_note = ""

    # show date range once before any section output
    if opts.start and opts.end:
        _day_range = opts.start.time() == _time.min and opts.end.time() == _time.max
        if _day_range:
            range = f" {compact_date_range(opts.start, opts.end)}"
            start_str = opts.start.strftime("%Y-%m-%d (%A)")
            end_str = opts.end.strftime("%Y-%m-%d (%A)")
            same = opts.start.date() == opts.end.date()
        else:
            range = f" {opts.start.isoformat()}->{opts.end.isoformat()}"
            start_str = opts.start.isoformat()
            end_str = opts.end.isoformat()
            same = opts.start == opts.end

        if same:
            range_verbose = f"on {start_str}"
        else:
            range_verbose = f"between {start_str} and {end_str} inclusive"

        triage_task_name = reverse_triage_task_day(opts.start, opts.end)

        if triage_task_name:
            triage_task_note = f' ("{triage_task_name}")'

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"Triage{range} for team {opts.team!r}", file=output_cfg.out)

    if range_verbose:
        match output_cfg.fmt:
            case OutputFormat.TERMINAL:
                print(f"Items updated {range_verbose}{triage_task_note}...", file=output_cfg.out)
                print(file=output_cfg.out)
            case OutputFormat.MARKDOWN:
                print(f"Items updated {range_verbose}\n", file=output_cfg.out)
            case _:
                raise NotImplementedError

    fetch_tasks: dict[str, asyncio.Task[TriageResult]] = {}
    for source in opts.sources:
        fetch_tasks[source.name] = asyncio.create_task(source.find(config, opts, FetchMode.triage))

    results = await _output_results(output_cfg, fetch_tasks)

    # create markdown template
    if output_cfg.markdown_path:
        output_cfg.markdown_path.write_text(f"# Triage{range}\n")

        # ensure the section order
        result_map = dict(results)
        for source in ("launchpad", "github", "discourse", "proposed"):
            if source not in result_map:
                continue
            r = result_map[source]
            await r.write_markdown(output_cfg.markdown_path)

        logging.info("Markdown written to %s", output_cfg.markdown_path)


async def run_todo(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
    subscribed: bool = False,
) -> None:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter.

    All sources in *opts.sources* are optional — pass a subset to fetch only
    that source.  *subscribed* only controls LP fetch mode (subscription list
    vs. todo tag); GitHub is filtered by label regardless.
    """
    mode = FetchMode.subscribed if subscribed else FetchMode.todo

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"bug housekeeping for team {opts.team!r}\n")

    fetch_tasks: dict[str, asyncio.Task[TriageResult]] = {}
    for source in opts.sources:
        fetch_tasks[source.name] = asyncio.create_task(source.find(config, opts, mode))

    if output_cfg.bug_persistor is not None:
        results = await _output_results(output_cfg, fetch_tasks)
        for _, result in results:
            await result.record(output_cfg.bug_persistor)

        output_cfg.bug_persistor.save()


async def _output_results(
    output_cfg: OutputConfig, fetch_tasks: dict[str, asyncio.Task[TriageResult]]
) -> list[tuple[str, TriageResult]]:
    async with Spinner(set(fetch_tasks.keys())) as spinner:
        results = await asyncio.gather(
            *[_await_and_print(output_cfg, source, task, spinner) for source, task in fetch_tasks.items()]
        )
        return results


# Print sections in canonical order as each completes
async def _await_and_print(
    output_cfg: OutputConfig, source: str, task: asyncio.Task, spinner: Spinner
) -> tuple[str, TriageResult]:
    result: TriageResult = await task
    spinner.done(source)
    spinner.clear()
    spinner.suspend()  # prevent spinner redraws while section output is in progress
    try:
        await result.print_section(output_cfg)
        print(file=output_cfg.out)
    finally:
        spinner.resume()
    return source, result
