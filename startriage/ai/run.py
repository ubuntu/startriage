"""End-to-end orchestration that wires the AI layer to the CLI.

Two entry points feed bugs to the agent and render a report:

- :func:`gather_user_bug_payloads` resolves user-supplied bug specs (URL,
  ``NNNNNN`` or ``#NNNNNN``) into agent payloads (``analyze``).
- :func:`payloads_from_tasks` turns already-fetched triage tasks into payloads
  (``triage --ai``).

Both hand their payloads to :func:`run_agent_on_payloads`, which runs the agent
sequentially and returns the rendered markdown; emitting it (printing or folding
it into a triage markdown file) is left to the caller. :func:`describe_bug_specs`
shares the same gather step but only renders the raw bug metadata (``analyze``
without ``--ai``). Launchpad access is lazily imported inside the gather helpers
so non-AI commands and offline tests never pull in launchpadlib.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import platformdirs

from ..config import StarTriageConfig, resolve_team_name
from ..spinner import Spinner
from .checks import URLChecker
from .feedback import default_feedback_log
from .pipeline import Pipeline, triage_bugs
from .precheck import Window
from .provider import Provider, build_provider
from .render import render_bug_metadata, render_report
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


def _make_spinner(total: int) -> Spinner:
    """Return a status spinner for the triage run.

    The spinner is a no-op when stderr is not a TTY (piped/CI), so callers can
    always use it unconditionally; that TTY handling lives in :class:`Spinner`.
    """
    noun = "bug" if total == 1 else "bugs"
    return Spinner(set(), status=f"Preparing to triage {total} {noun}…")


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
def scratch_dir(keep: bool = False) -> Iterator[Path]:
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


async def run_agent_on_payloads(
    config: StarTriageConfig,
    payloads: list[dict[str, Any]],
    *,
    lp: Launchpad | None = None,
    provider: Provider | None = None,
    team: frozenset[str] = frozenset(),
    window: Window | None = None,
    keep_workdir: bool = False,
) -> str | None:
    """Run the agent over ``payloads`` and return the rendered markdown report.

    Returns the markdown string, or ``None`` when there is nothing to triage.
    Emitting the report (printing it or folding it into a triage markdown file)
    is left to the caller. When ``provider`` is omitted it is built from
    ``config`` (validating credentials, which may raise
    :class:`~startriage.config.AIConfigError`). The agent gets the tools enabled
    in ``[ai.tools]``; Launchpad tools use ``lp``. ``team`` (member names) and
    ``window`` (the triage date range) drive the precheck routes. The scratch
    directory (fetched sources, session files) is kept with ``keep_workdir``.
    """
    if not payloads:
        logger.info("No bugs to triage with the AI agent.")
        return None

    if provider is None:
        provider = build_provider(config.ai)

    tool_names = enabled_tool_names(config.ai)
    logger.info("AI agent tools: %s", ", ".join(tool_names) or "none")
    spinner = _make_spinner(len(payloads))
    current = {"label": "bug", "count": ""}

    def on_progress(index: int, total: int, bug: str) -> None:
        current["label"] = f"LP #{bug}" if bug else "bug"
        current["count"] = f"{index}/{total}"
        spinner.set_status(f"Triaging {current['label']} ({current['count']})…")

    def on_phase(phase: str) -> None:
        spinner.set_status(f"Triaging {current['label']} · {phase} ({current['count']})…")

    async with aiohttp.ClientSession() as http_session, spinner:
        http = ReadOnlyHTTP(
            http_session,
            allowed_posts=debian.ALLOWED_POSTS,
            credentials=_github_credentials(config) if "github_search" in tool_names else None,
        )
        with scratch_dir(keep_workdir) as workdir:
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
                model=config.ai.model or "",
                working_directory=workdir,
                on_phase=on_phase,
            )
            outcomes = await triage_bugs(pipeline, payloads, on_progress=on_progress)

    return render_report(outcomes, tools=tool_names)


async def run_ai_over_bug_specs(
    config: StarTriageConfig,
    lp: Launchpad,
    bug_specs: list[str],
    *,
    provider: Provider | None = None,
    keep_workdir: bool = False,
) -> str | None:
    """Resolve user-supplied bug specs and run the agent, returning the report.

    Returns the rendered markdown, or ``None`` when no valid bug could be
    resolved from ``bug_specs``. Launchpad access runs off-thread so the async
    event loop is not blocked.
    """
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
    return await run_agent_on_payloads(
        config, payloads, lp=lp, provider=provider, team=team, keep_workdir=keep_workdir
    )


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


async def run_ai_over_triage_results(
    config: StarTriageConfig,
    results: Mapping[str, TriageResult],
    *,
    provider: Provider | None = None,
    keep_workdir: bool = False,
) -> str | None:
    """Run the agent over the Launchpad tasks gathered by a normal triage run.

    Returns the rendered markdown, or ``None`` when there are no Launchpad tasks
    to triage. Launchpad payload construction runs off-thread.
    """
    from ..sources.launchpad.triage import LaunchpadTriage

    result = results.get("launchpad")
    if not isinstance(result, LaunchpadTriage):
        return await run_agent_on_payloads(config, [], provider=provider)

    payloads = await asyncio.to_thread(payloads_from_tasks, list(result.tasks.tasks))
    lp = result.tasks.lp
    team = await asyncio.to_thread(team_members, lp, result.team_config.lp_team) if payloads else frozenset()
    window = (result.filter.start, result.filter.end) if result.filter.start and result.filter.end else None
    return await run_agent_on_payloads(
        config, payloads, lp=lp, provider=provider, team=team, window=window, keep_workdir=keep_workdir
    )
