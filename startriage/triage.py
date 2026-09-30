"""Library entry point: select and fetch triage data from all sources, without rendering."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from .config import StarTriageConfig, UpdateFilter, resolve_team_name
from .dates import parse_interval, triage_task_date_range
from .output import FailedTriageResult, TriageResult
from .source import FetchMode, TaskFilterOptions, TriageSource
from .sources.discourse.triage import find as discourse_find
from .sources.github.triage import find as github_find
from .sources.launchpad.triage import find as launchpad_find
from .sources.proposed.triage import find as proposed_find

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


def build_filter(
    config: StarTriageConfig,
    team: str | None = None,
    interval: str | None = None,
    triage_day: str | None = None,
    sources: str | None = None,
    source_filter: set[str] | None = None,
    flag_recent: int = 7,
    flag_old: int = 30,
    show_expiration: bool = True,
    update_filter: UpdateFilter | None = None,
) -> TaskFilterOptions:
    """Build filter options from CLI-style values; see ``startriage triage --help`` for their meaning."""
    if interval and triage_day:
        raise ValueError("interval and triage_day are mutually exclusive")

    if interval:
        start, end = parse_interval(interval)
    else:
        start, end = triage_task_date_range(triage_day)

    now = datetime.now(timezone.utc)

    return TaskFilterOptions(
        team=resolve_team_name(team, config),
        start=start,
        end=end,
        recent_since=now - timedelta(days=flag_recent),
        old_since=now - timedelta(days=flag_old),
        sources=resolve_sources(sources, source_filter),
        show_expiration=show_expiration,
        update_filter=update_filter,
    )


async def fetch(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    mode: FetchMode = FetchMode.triage,
    on_result: Callable[[str, TriageResult], Awaitable[None]] | None = None,
) -> dict[str, TriageResult]:
    """Fetch all sources in *opts* concurrently, without rendering anything.

    Sources whose fetch raised come back as ``FailedTriageResult`` with ``.error`` set.
    *on_result* is awaited with each result as soon as its source is done.
    Nothing prompts the user: Launchpad fails without stored credentials (``startriage login launchpad``).

    Launchpad ``Task`` properties may query Launchpad synchronously on access;
    ``await result.to_dict()`` does that off the event loop.
    """
    sources = list(opts.sources)

    async def fetch_one(source: TriageSource) -> TriageResult:
        try:
            result = await source.find(config, opts, mode)
        except Exception as exc:
            result = FailedTriageResult(exc)
        if on_result is not None:
            await on_result(source.name, result)
        return result

    results = await asyncio.gather(*map(fetch_one, sources))
    return {s.name: r for s, r in zip(sources, results, strict=True)}
