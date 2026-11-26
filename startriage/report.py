"""Render triage and todo reports to an output stream (terminal, markdown, json)."""

from __future__ import annotations

import io
import json
import logging
import sys
import traceback
from collections.abc import Mapping
from datetime import date, time

from .analysis import AIRun
from .config import StarTriageConfig
from .dates import compact_date_range, last_day, reverse_triage_task_day
from .htmlreport import write_report
from .output import FailedTriageResult, OutputConfig, OutputFormat, TriageResult, json_default
from .source import FetchMode, TaskFilterOptions
from .spinner import Spinner
from .triage import fetch

# section order in the markdown template
_MARKDOWN_SOURCES = ("launchpad", "github", "discourse", "proposed")


def _triage_range_labels(opts: TaskFilterOptions) -> tuple[str, str, str]:
    """Return ``(compact_range, range_verbose, triage_task_note)`` for ``opts``."""
    range_str = range_verbose = triage_task_note = ""
    if opts.start and opts.end:
        first, last = opts.start.date(), last_day(opts.end)
        if opts.start.time() == time.min and opts.end.time() == time.min:
            range_str = f" {compact_date_range(first, last)}"
            first_str, last_str = (d.strftime("%Y-%m-%d (%A)") for d in (first, last))
            if first == last:
                range_verbose = f"on {first_str}"
            else:
                range_verbose = f"between {first_str} and {last_str} inclusive"
        else:
            range_str = f" {opts.start.isoformat()}->{opts.end.isoformat()}"
            range_verbose = f"from {opts.start.isoformat()} until {opts.end.isoformat()}"

        triage_task_name = reverse_triage_task_day(first, last)
        if triage_task_name:
            triage_task_note = f' ("{triage_task_name}")'
    return range_str, range_verbose, triage_task_note


async def gather_triage(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
) -> dict[str, TriageResult]:
    """Stage 1 of daily triage: fetch all sources concurrently and stream terminal/markdown output.

    Does not emit ``--format json`` or ``--markdown``/``--html`` report files;
    call :func:`emit_triage_outputs` once any optional AI analysis has completed.
    """
    range_str, range_verbose, triage_task_note = _triage_range_labels(opts)

    if output_cfg.fmt == OutputFormat.TERMINAL:
        print(f"Triage{range_str} for team {opts.team!r}", file=output_cfg.out)

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

    return await _fetch_and_render(config, opts, FetchMode.triage, output_cfg, emit_json=False)


async def emit_triage_outputs(
    results: Mapping[str, TriageResult],
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
    *,
    ai_run: AIRun | None = None,
    ai_markdown: str | None = None,
) -> None:
    """Stage 3 of daily triage: write JSON, markdown, and HTML outputs (including AI if run)."""
    range_str, range_verbose, _ = _triage_range_labels(opts)

    if output_cfg.fmt == OutputFormat.JSON:
        data: dict[str, object] = {source: await result.to_dict() for source, result in results.items()}
        if ai_run is not None:
            data["ai"] = ai_run.to_dict()
        json.dump(data, output_cfg.out, indent=2, default=json_default)
        print(file=output_cfg.out)

    if output_cfg.markdown_path or output_cfg.html_path:
        markdown = await _triage_markdown(results, range_str)

        if output_cfg.markdown_path:
            full_md = markdown
            if ai_markdown:
                full_md = markdown + "\n\n" + ai_markdown
            output_cfg.markdown_path.write_text(full_md, encoding="utf-8")
            logging.info("Markdown written to %s", output_cfg.markdown_path)

        if output_cfg.html_path:
            subtitle = f"team {opts.team}" + (f", items updated {range_verbose}" if range_verbose else "")
            days = dict.fromkeys(d.isoformat() for d in (opts.start.date(), last_day(opts.end)))
            filename = "-".join(["triage", *days]) + ".md"
            await write_report(
                output_cfg.html_path,
                results,
                FetchMode.triage,
                f"Triage{range_str}",
                subtitle,
                markdown=markdown,
                filename=filename,
                ai_run=ai_run,
            )
            logging.info("HTML written to %s", output_cfg.html_path)

    # When AI ran without a file target (or JSON format), print the AI markdown to stdout.
    if (
        ai_markdown
        and output_cfg.fmt != OutputFormat.JSON
        and output_cfg.markdown_path is None
        and output_cfg.html_path is None
    ):
        print("\n" + ai_markdown, file=output_cfg.out)


async def triage(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    output_cfg: OutputConfig,
) -> dict[str, TriageResult]:
    """Daily triage: gather all sources and emit all configured outputs."""
    results = await gather_triage(config, opts, output_cfg)
    await emit_triage_outputs(results, opts, output_cfg)
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

    # before saving, which would make today's file the next compare file
    if output_cfg.html_path:
        persistor = output_cfg.bug_persistor
        subtitle = f"team {opts.team}"
        if persistor and persistor.compare_path:
            subtitle += f", compared with {persistor.compare_path.name}"
        filename = f"todo-{date.today().isoformat()}.md"
        await write_report(
            output_cfg.html_path, results, mode, "Bug housekeeping", subtitle, persistor, filename=filename
        )
        logging.info("HTML written to %s", output_cfg.html_path)

    if output_cfg.bug_persistor is not None:
        for result in results.values():
            await result.record(output_cfg.bug_persistor)

        output_cfg.bug_persistor.save()

    return results


async def _triage_markdown(results: Mapping[str, TriageResult], range: str) -> str:
    """Markdown template of the triage report, e.g. for a Discourse post."""
    buf = io.StringIO()
    buf.write(f"# Triage of changes on{range}\n" if range else "# Triage\n")

    md_cfg = OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf, open_in_browser=False, terminal_links=False)

    # skip sources that failed to fetch
    for source in _MARKDOWN_SOURCES:
        result = results.get(source)
        if result is None or result.error is not None:
            continue
        await result.print_section(md_cfg)
        buf.write("\n")

    return buf.getvalue()


def print_fetch_errors(results: Mapping[str, TriageResult]) -> bool:
    """Print tracebacks of sources whose fetch raised; return True if any fetch failed.

    The one-line summary of each error is printed as soon as its source fails, see ``_fetch_and_render``.
    """
    failed = False
    for source, result in results.items():
        if result.error is None:
            continue
        failed = True
        if isinstance(result.error, Exception):
            print(f"\nTraceback of fetching {source!r}:", file=sys.stderr)
            traceback.print_exception(result.error, file=sys.stderr)
    return failed


async def _fetch_and_render(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    mode: FetchMode,
    output_cfg: OutputConfig,
    *,
    emit_json: bool = True,
) -> dict[str, TriageResult]:
    """Fetch and render sections as they complete, so we don't wait for the slowest source.

    JSON can't be streamed per section, so it is written once all sources are done
    (unless ``emit_json=False``, when the caller emits JSON later).
    Reporting of fetch errors is left to the caller; see ``print_fetch_errors``.
    """
    async with Spinner({s.name for s in opts.sources}) as spinner:

        async def render(source: str, result: TriageResult) -> None:
            spinner.done(source)
            if isinstance(result, FailedTriageResult):
                spinner.clear()
                print(f"Error fetching {source!r}: {result.message.splitlines()[0]}", file=sys.stderr)
                return
            if output_cfg.fmt == OutputFormat.JSON:
                return

            spinner.clear()
            spinner.suspend()  # prevent spinner redraws while section output is in progress
            try:
                await result.print_section(output_cfg)
                print(file=output_cfg.out)
            finally:
                spinner.resume()

        results = await fetch(config, opts, mode, on_result=render)

    if emit_json and output_cfg.fmt == OutputFormat.JSON:
        data = {source: await result.to_dict() for source, result in results.items()}
        json.dump(data, output_cfg.out, indent=2, default=json_default)
        print(file=output_cfg.out)

    return results
