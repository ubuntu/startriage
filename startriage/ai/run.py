"""End-to-end orchestration that wires the AI layer to the CLI.

Work is structured in two steps:

- Collect :class:`~startriage.analysis.WorkItem` objects (each carrying its
  tracker state snapshot and raw payload) via :func:`gather_user_work_items`
  (``analyze``) or :func:`collect_triage_work_items` (``triage --ai``).
- Analyze each item sequentially with :func:`analyze_work_items`, producing a
  structured :class:`~startriage.analysis.AIRun` (plus the raw
  :class:`~startriage.ai.agent.BugOutcome` list for full markdown rendering).

Launchpad access is lazily imported inside the gather helpers so non-AI
commands and offline tests never pull in launchpadlib.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import aiohttp
import platformdirs

from ..analysis import AIRun, ItemAnalysis, ItemSnapshot, PhaseCost, WorkItem
from ..config import StarTriageConfig, resolve_team_name
from .agent import BugOutcome
from .checks import URLChecker
from .feedback import default_feedback_log
from .pipeline import Pipeline, phase_totals, triage_one
from .precheck import Window
from .progress import AIProgress
from .provider import Provider, build_provider
from .render import render_bug_metadata, render_report
from .results import to_item_analysis
from .tools import ReadOnlyHTTP, ToolContext, build_tools, debian, enabled_tool_names

if TYPE_CHECKING:
    from collections.abc import Mapping

    from launchpadlib.launchpad import Launchpad

    from ..output import TriageResult
    from ..sources.launchpad.models import Task

logger = logging.getLogger(__name__)

# A bare bug number, optionally ``#``-prefixed.
_BARE_BUG = re.compile(r"^#?(\d+)$")
# A genuine Launchpad bug reference inside a URL (``.../+bug/<n>`` or ``.../bugs/<n>``).
_URL_BUG = re.compile(r"launchpad\.net/(?:.*/)?(?:\+bug|bugs)/(\d+)", re.IGNORECASE)


@dataclass
class AITriageBatch:
    """Result of an AI run: structured :class:`AIRun` plus raw outcomes for markdown rendering."""

    run: AIRun
    outcomes: list[BugOutcome] = field(default_factory=list)

    def to_markdown(self) -> str:
        return render_report(self.outcomes, tools=self.run.tools)


def parse_bug_number(spec: str) -> str:
    """Extract a Launchpad bug number from a bare ``NNNNNN``, ``#NNNNNN`` or LP URL.

    Only real Launchpad bug references are accepted. Arbitrary URLs or text that
    merely happen to contain digits (e.g. ``https://example.com/pages/3133742``)
    raise :class:`ValueError` rather than silently resolving to a wrong number.
    """
    spec = spec.strip()
    bare = _BARE_BUG.match(spec)
    if bare:
        return bare.group(1)
    url = _URL_BUG.search(spec)
    if url:
        return url.group(1)
    raise ValueError(f"could not parse a Launchpad bug number from {spec!r}")


def work_item_from_payload(payload: dict[str, Any], *, source: str = "launchpad") -> WorkItem:
    """Wrap a raw bug payload as a :class:`WorkItem` with its current tracker snapshot."""
    number = str(payload.get("number", ""))
    series: dict[str, str] = {}
    for affected in payload.get("affected", []) or []:
        name = affected.get("series")
        status = affected.get("status")
        if name and status:
            series[name] = status
    return WorkItem(
        source=source,
        key=number,
        url=payload.get("url") or (f"https://bugs.launchpad.net/bugs/{number}" if number else ""),
        package=payload.get("package") or "",
        title=payload.get("short_title") or payload.get("title") or "",
        state=ItemSnapshot(
            status=payload.get("status") or "",
            importance=payload.get("importance") or "",
            series=series,
            tags=list(payload.get("tags") or []),
            assignees=list(payload.get("assignees") or []),
            expiring=bool(payload.get("expiring")),
        ),
        payload=payload,
    )


def gather_user_bug_payloads(lp: Launchpad, bug_specs: list[str]) -> list[dict[str, Any]]:
    """Resolve user-supplied bug specs into agent payloads (blocking LP access)."""
    from ..sources.launchpad.models import Task

    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in bug_specs:
        number = parse_bug_number(spec)
        if number in seen:
            continue
        seen.add(number)
        try:
            bug_tasks = list(lp.bugs[number].bug_tasks)
            if not bug_tasks:
                logger.warning("Skipping bug %s: no bug tasks found", number)
                continue
            task = Task(bug_tasks[0], last_activity_ours=False)
            payloads.append(task.to_agent_payload())
        except Exception:
            logger.warning("Skipping bug %s: failed to fetch", number, exc_info=True)
    return payloads


def payloads_from_tasks(tasks: list[Task]) -> list[dict[str, Any]]:
    """Build agent payloads from already-fetched tasks (blocking LP access).

    Tasks are de-duplicated by bug number so a bug with multiple affected
    targets is triaged once.
    """
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in tasks:
        if task.number in seen:
            continue
        seen.add(task.number)
        try:
            payloads.append(task.to_agent_payload())
        except Exception:
            logger.warning("Skipping bug %s: failed to build payload", task.number, exc_info=True)
    return payloads


def work_items_from_tasks(tasks: list[Task]) -> list[WorkItem]:
    """Build :class:`WorkItem` objects (with tracker state snapshots) from Launchpad tasks."""
    return [work_item_from_payload(p) for p in payloads_from_tasks(tasks)]


def team_members(lp: Launchpad, team: str) -> frozenset[str]:
    """Launchpad names of the team and its members (blocking LP access)."""
    try:
        people = lp.people[team]
        return frozenset({team, *(person.name for person in people.participants)})
    except Exception:
        logger.warning(
            "Could not read the members of %s; no activity counts as the team's", team, exc_info=True
        )
        return frozenset()


def _github_credentials(config: StarTriageConfig) -> dict[str, dict[str, str]] | None:
    """The GitHub API token header, sent to api.github.com only (higher search limits)."""
    from ..sources.github.auth import get_github_token

    try:
        token = get_github_token(config.general.github_token)
    except Exception as exc:
        logger.info("No GitHub token for the AI tools: %s", exc)
        return None
    return {"api.github.com": {"Authorization": f"Bearer {token}"}} if token else None


@contextlib.contextmanager
def scratch_dir(keep: bool = False) -> Generator[Path]:
    """A per-run working directory for the agent session, removed afterwards unless ``keep``."""
    root = platformdirs.user_cache_path("startriage") / "ai"
    root.mkdir(parents=True, exist_ok=True)
    if keep:
        path = Path(tempfile.mkdtemp(dir=root))
        try:
            yield path
        finally:
            logger.warning("Kept the AI scratch directory: %s", path)
        return
    with tempfile.TemporaryDirectory(dir=root) as name:
        yield Path(name)


@dataclass(frozen=True)
class RunOptions:
    """Display and scratch-directory options for an AI run."""

    keep_workdir: bool = False
    verbose: bool = False
    show_thinking: bool = False
    progress_stream: IO[str] | None = None
    progress_tty: bool | None = None


async def analyze_work_items(
    config: StarTriageConfig,
    items: list[WorkItem],
    *,
    lp: Launchpad | None = None,
    provider: Provider | None = None,
    team: frozenset[str] = frozenset(),
    window: Window | None = None,
    options: RunOptions | None = None,
) -> AITriageBatch | None:
    """Analyze each :class:`WorkItem` sequentially and return an :class:`AITriageBatch`.

    Returns ``None`` when ``items`` is empty. Live progress (and optional
    thinking text when ``options.show_thinking`` is enabled) is streamed to
    ``options.progress_stream`` (default stderr); each finished item leaves its
    summary line on the terminal so the next item starts on a new line.
    """
    if not items:
        logger.info("No bugs to triage with the AI agent.")
        return None

    opts = options or RunOptions()
    if provider is None:
        provider = build_provider(config.ai)

    tool_names = enabled_tool_names(config.ai)
    logger.info("AI agent tools: %s", ", ".join(tool_names) or "none")
    progress = AIProgress(
        len(items),
        verbose=opts.verbose,
        show_thinking=opts.show_thinking,
        stream=opts.progress_stream,
        is_tty=opts.progress_tty,
    )

    outcomes: list[BugOutcome] = []
    analyses: list[ItemAnalysis] = []

    async with aiohttp.ClientSession() as http_session, progress:
        http = ReadOnlyHTTP(
            http_session,
            allowed_posts=debian.ALLOWED_POSTS,
            credentials=_github_credentials(config) if "github_search" in tool_names else None,
        )
        with scratch_dir(opts.keep_workdir) as workdir:
            tools = build_tools(ToolContext(lp=lp, http=http, workdir=workdir), tool_names)
            pipeline = Pipeline(
                provider=provider,
                tools={tool.name: tool for tool in tools},
                lp=lp,
                http=http,
                team=team,
                window=window,
                urls=URLChecker(http if "verify_url" in tool_names else None),
                feedback=config.ai.suggest_improvements,
                feedback_log=default_feedback_log() if config.ai.suggest_improvements else None,
                model=config.ai.model or provider.model,
                working_directory=workdir,
                streaming=opts.show_thinking,
                on_phase=progress.on_phase_start,
                on_phase_done=progress.on_phase_done,
                on_event=progress.on_event,
            )
            total = len(items)
            for index, item in enumerate(items, start=1):
                progress.on_item_start(index, total, item)
                logger.info("Triaging bug %s (%d/%d)…", item.key, index, total)
                outcome = await triage_one(pipeline, item.payload)
                analysis = to_item_analysis(outcome, item)
                progress.on_item_done(index, total, analysis)
                outcomes.append(outcome)
                analyses.append(analysis)

    totals = {
        name: PhaseCost(sessions=s, input_tokens=i, output_tokens=o)
        for name, (s, i, o) in phase_totals(outcomes).items()
    }
    run = AIRun(
        model=config.ai.model or provider.model,
        tools=tool_names,
        items=analyses,
        totals=totals,
    )
    return AITriageBatch(run=run, outcomes=outcomes)


async def run_agent_on_payloads(
    config: StarTriageConfig,
    payloads: list[dict[str, Any]],
    *,
    lp: Launchpad | None = None,
    provider: Provider | None = None,
    team: frozenset[str] = frozenset(),
    window: Window | None = None,
    keep_workdir: bool = False,
    verbose: bool = False,
    show_thinking: bool = False,
) -> str | None:
    """Run the agent over ``payloads`` and return the rendered markdown report."""
    items = [work_item_from_payload(p) for p in payloads]
    batch = await analyze_work_items(
        config,
        items,
        lp=lp,
        provider=provider,
        team=team,
        window=window,
        options=RunOptions(keep_workdir=keep_workdir, verbose=verbose, show_thinking=show_thinking),
    )
    return batch.to_markdown() if batch is not None else None


async def analyze_bug_specs(
    config: StarTriageConfig,
    lp: Launchpad,
    bug_specs: list[str],
    *,
    provider: Provider | None = None,
    options: RunOptions | None = None,
) -> AITriageBatch | None:
    """Resolve user-supplied bug specs into WorkItems and run AI analysis."""
    payloads = await asyncio.to_thread(gather_user_bug_payloads, lp, bug_specs)
    if not payloads:
        return None
    team: frozenset[str] = frozenset()
    try:
        lp_team = config.get_team(resolve_team_name(None, config)).lp_team
    except KeyError:
        logger.info("No default team: no activity counts as the team's")
    else:
        team = await asyncio.to_thread(team_members, lp, lp_team)
    items = [work_item_from_payload(p) for p in payloads]
    return await analyze_work_items(config, items, lp=lp, provider=provider, team=team, options=options)


async def run_ai_over_bug_specs(
    config: StarTriageConfig,
    lp: Launchpad,
    bug_specs: list[str],
    *,
    provider: Provider | None = None,
    keep_workdir: bool = False,
    verbose: bool = False,
    show_thinking: bool = False,
) -> str | None:
    """Resolve user-supplied bug specs and run the agent, returning the report.

    Returns the rendered markdown, or ``None`` when no valid bug could be
    resolved from ``bug_specs``. Launchpad access runs off-thread so the async
    event loop is not blocked.
    """
    batch = await analyze_bug_specs(
        config,
        lp,
        bug_specs,
        provider=provider,
        options=RunOptions(keep_workdir=keep_workdir, verbose=verbose, show_thinking=show_thinking),
    )
    return batch.to_markdown() if batch is not None else None


async def describe_bug_specs(lp: Launchpad, bug_specs: list[str]) -> str | None:
    """Resolve user-supplied bug specs and render their metadata (no AI agent).

    Returns the rendered markdown, or ``None`` when no valid bug could be
    resolved from ``bug_specs``. Launchpad access runs off-thread so the async
    event loop is not blocked.
    """
    payloads = await asyncio.to_thread(gather_user_bug_payloads, lp, bug_specs)
    if not payloads:
        return None
    return render_bug_metadata(payloads)


async def analyze_triage_results(
    config: StarTriageConfig,
    results: Mapping[str, TriageResult],
    *,
    provider: Provider | None = None,
    options: RunOptions | None = None,
) -> AITriageBatch | None:
    """Collect WorkItems (including expiring bugs) from triage results and run AI analysis."""
    from ..sources.launchpad.triage import LaunchpadTriage

    result = results.get("launchpad")
    if not isinstance(result, LaunchpadTriage):
        return await analyze_work_items(config, [], provider=provider, options=options)

    all_tasks = [*result.tasks.tasks, *result.tasks.expire_level1, *result.tasks.expire_level2]
    items = await asyncio.to_thread(work_items_from_tasks, all_tasks)
    lp = result.tasks.lp
    team = await asyncio.to_thread(team_members, lp, result.team_config.lp_team) if items else frozenset()
    window = (result.filter.start, result.filter.end) if result.filter.start and result.filter.end else None
    return await analyze_work_items(
        config,
        items,
        lp=lp,
        provider=provider,
        team=team,
        window=window,
        options=options,
    )


async def run_ai_over_triage_results(
    config: StarTriageConfig,
    results: Mapping[str, TriageResult],
    *,
    provider: Provider | None = None,
    keep_workdir: bool = False,
    verbose: bool = False,
    show_thinking: bool = False,
) -> str | None:
    """Run the agent over the Launchpad tasks gathered by a normal triage run.

    Returns the rendered markdown, or ``None`` when there are no Launchpad tasks
    to triage.
    """
    batch = await analyze_triage_results(
        config,
        results,
        provider=provider,
        options=RunOptions(keep_workdir=keep_workdir, verbose=verbose, show_thinking=show_thinking),
    )
    return batch.to_markdown() if batch is not None else None
