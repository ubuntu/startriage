"""Render triage and todo reports to an output stream (terminal, markdown, json)."""

from __future__ import annotations

import io
import json
import logging
import sys
import traceback
from collections.abc import Mapping
from datetime import time

from .config import StarTriageConfig
from .dates import compact_date_range, reverse_triage_task_day
from .enums import FetchMode
from .output import OutputConfig, OutputFormat, TriageResult, json_default
from .source import TaskFilterOptions
from .spinner import Spinner
from .triage import fetch

# section order in the markdown template
_MARKDOWN_SOURCES = ("launchpad", "github", "discourse", "proposed")


async def triage(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
) -> dict[str, TriageResult]:
    """Daily triage: fetch all sources concurrently, print sections in order as they complete.

    Returns the results so callers (e.g. ``triage --ai``) can reuse them without re-fetching.
    """

    range = range_verbose = triage_task_note = ""

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
            case OutputFormat.JSON:
                pass
            case _:
                raise NotImplementedError

    results = await _fetch_and_render(config, opts, FetchMode.triage, output_cfg)

    # create markdown template
    if output_cfg.markdown_path:
        buf = io.StringIO()

        if range:
            buf.write(f"# Triage of changes on{range}\n")
        else:
            buf.write("# Triage\n")

        md_cfg = OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf, open_in_browser=False, terminal_links=False)

        # skip sources that failed to fetch
        for source in _MARKDOWN_SOURCES:
            result = results.get(source)
            if result is None or result.error is not None:
                continue
            await result.print_section(md_cfg)
            buf.write("\n")

        with output_cfg.markdown_path.open("w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())

        logging.info("Markdown written to %s", output_cfg.markdown_path)

    return results


async def todo(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
    subscribed: bool = False,
) -> dict[str, TriageResult]:
    """Todo / housekeeping triage: tag-filtered bugs, no date filter.

    *subscribed* only controls the Launchpad fetch mode (subscription list vs. todo tag);
    GitHub is filtered by label regardless.
    """
    mode = FetchMode.subscribed if subscribed else FetchMode.todo

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"bug housekeeping for team {opts.team!r}\n", file=output_cfg.out)

    results = await _fetch_and_render(config, opts, mode, output_cfg)

    if output_cfg.bug_persistor is not None:
        for result in results.values():
            await result.record(output_cfg.bug_persistor)

        output_cfg.bug_persistor.save()

    return results


def print_fetch_errors(results: Mapping[str, TriageResult]) -> bool:
    """Print errors for sources whose fetch failed; return True if any failed.

    String errors are shown compactly on one line; exceptions get a full traceback.
    """
    failed = False
    for source, result in results.items():
        if result.error is None:
            continue
        failed = True
        print(f"\nError fetching {source!r}:", file=sys.stderr)
        if isinstance(result.error, str):
            print(f"  {result.error}", file=sys.stderr)
        else:
            traceback.print_exception(result.error, file=sys.stderr)
    return failed


async def _fetch_and_render(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    mode: FetchMode,
    output_cfg: OutputConfig,
) -> dict[str, TriageResult]:
    """Fetch and render sections as they complete, so we don't wait for the slowest source.

    JSON can't be streamed per section, so it is written once all sources are done.
    Reporting of fetch errors is left to the caller; see ``print_fetch_errors``.
    """
    async with Spinner({s.name for s in opts.sources}) as spinner:

        async def render(source: str, result: TriageResult) -> None:
            spinner.done(source)
            if result.error is not None or output_cfg.fmt == OutputFormat.JSON:
                return

            spinner.clear()
            spinner.suspend()  # prevent spinner redraws while section output is in progress
            try:
                await result.print_section(output_cfg)
                print(file=output_cfg.out)
            finally:
                spinner.resume()

        results = await fetch(config, opts, mode, on_result=render)

    if output_cfg.fmt == OutputFormat.JSON:
        data = {source: await result.to_dict() for source, result in results.items()}
        json.dump(data, output_cfg.out, indent=2, default=json_default)
        print(file=output_cfg.out)

    return results
