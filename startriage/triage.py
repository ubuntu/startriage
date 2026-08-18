"""Generic entry point for all triage modes."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import traceback
from datetime import time

from .config import StarTriageConfig
from .dates import compact_date_range, reverse_triage_task_day
from .enums import FetchMode
from .output import FailedTriageResult, OutputConfig, OutputFormat, TriageResult
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
) -> list[tuple[str, TriageResult]]:
    """Daily triage: fetch all sources concurrently, print sections in order as they complete.

    Returns the ``(source_name, result)`` pairs so callers (e.g. ``triage --ai``)
    can reuse them without re-fetching.
    """

    range = triage_task_note = ""

    # show date range once before any section output
    if opts.start and opts.end:
        _day_range = opts.start.time() == time.min and opts.end.time() == time.max
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

    results = await _render_sections(output_cfg, fetch_tasks)

    # create markdown template
    if output_cfg.markdown_path:
        buf = io.StringIO()

        if range:
            buf.write(f"# Triage of changes on{range}\n")
        else:
            buf.write("# Triage\n")

        md_cfg = OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf, open_in_browser=False, terminal_links=False)

        # ensure the section order; skip sources that failed to fetch
        result_map = {s: r for s, r in results if r.error is None}
        for source in ("launchpad", "github", "discourse", "proposed"):
            if source not in result_map:
                continue
            r = result_map[source]
            await r.print_section(md_cfg)
            buf.write("\n")

        with output_cfg.markdown_path.open("w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())

        logging.info("Markdown written to %s", output_cfg.markdown_path)

    return results


async def run_todo(
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    output_cfg: OutputConfig,
    subscribed: bool = False,
) -> list[tuple[str, TriageResult]]:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter.

    All sources in *filter.sources* are optional — pass a subset to fetch only
    that source.  *subscribed* only controls LP fetch mode (subscription list
    vs. todo tag); GitHub is filtered by label regardless.

    Returns the ``(source_name, result)`` pairs; sources whose fetch raised are
    returned as ``FailedTriageResult`` carrying the exception in ``.error``.
    """
    mode = FetchMode.subscribed if subscribed else FetchMode.todo

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"bug housekeeping for team {filter.team!r}\n")

    fetch_tasks: dict[str, asyncio.Task[TriageResult]] = {}
    for source in filter.sources:
        fetch_tasks[source.name] = asyncio.create_task(source.find(config, filter, mode))

    results = await _render_sections(output_cfg, fetch_tasks)

    if output_cfg.bug_persistor is not None:
        for _, result in results:
            await result.record(output_cfg.bug_persistor)

        output_cfg.bug_persistor.save()

    return results


def print_fetch_errors(results: list[tuple[str, TriageResult]]) -> bool:
    """Print tracebacks for sources whose fetch failed; return True if any failed."""
    failed = False
    for source, result in results:
        if result.error is None:
            continue
        failed = True
        print(f"\nError fetching {source!r}:", file=sys.stderr)
        traceback.print_exception(result.error, file=sys.stderr)
    return failed


async def _render_sections(
    output_cfg: OutputConfig, fetch_tasks: dict[str, asyncio.Task[TriageResult]]
) -> list[tuple[str, TriageResult]]:
    """Render sections as fetches complete; failed fetches come back as ``FailedTriageResult``.

    Reporting of fetch errors is left to the caller; see ``print_fetch_errors``.
    """
    async with Spinner(set(fetch_tasks.keys())) as spinner:
        return await asyncio.gather(
            *[_await_and_print(output_cfg, source, task, spinner) for source, task in fetch_tasks.items()]
        )


# Print sections as each completes, so we don't have to wait for the slowest source
async def _await_and_print(
    output_cfg: OutputConfig, source: str, task: asyncio.Task, spinner: Spinner
) -> tuple[str, TriageResult]:
    try:
        result: TriageResult = await task
    except Exception as exc:
        spinner.done(source)
        return source, FailedTriageResult(exc)

    spinner.done(source)
    spinner.clear()
    spinner.suspend()  # prevent spinner redraws while section output is in progress
    try:
        await result.print_section(output_cfg)
        print(file=output_cfg.out)
    finally:
        spinner.resume()
    return source, result
