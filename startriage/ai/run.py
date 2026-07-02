"""End-to-end orchestration that wires the AI layer to the CLI.

Two entry points feed bugs to the agent and write a dated report:

- :func:`gather_user_bug_payloads` resolves user-supplied bug specs (URL,
  ``NNNNNN`` or ``#NNNNNN``) into agent payloads (``ai-triage``).
- :func:`payloads_from_tasks` turns already-fetched triage tasks into payloads
  (``triage --ai``).

Both hand their payloads to :func:`run_agent_on_payloads`, which runs the agent
sequentially and writes ``autotriage-YYYY-MM-DD.md``. Launchpad access is lazily
imported inside the gather helpers so non-AI commands and offline tests never
pull in launchpadlib.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

from ..config import StarTriageConfig
from ..spinner import Spinner
from .agent import load_system_prompt, triage_bugs
from .provider import Provider, build_provider
from .render import render_report

if TYPE_CHECKING:
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


def gather_user_bug_payloads(bug_specs: list[str]) -> list[dict[str, Any]]:
    """Resolve user-supplied bug specs into agent payloads (blocking LP access)."""
    from ..sources.launchpad.finder import connect_launchpad
    from ..sources.launchpad.models import Task

    lp = connect_launchpad()
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
            task = Task(bug_tasks[0], subscribed=False, last_activity_ours=False)
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


def _make_spinner(total: int) -> Spinner | None:
    """Return a status spinner, or ``None`` when one would be unhelpful/noisy.

    Suppressed when stderr is not a TTY (piped/CI) or when INFO logging is on
    (``-v``/``-vv``), since the agent loop already logs per-bug progress there
    and a redrawing spinner would corrupt the log stream.
    """
    if not sys.stderr.isatty():
        return None
    if logging.getLogger("startriage").isEnabledFor(logging.INFO):
        return None
    noun = "bug" if total == 1 else "bugs"
    return Spinner(set(), status=f"Preparing to triage {total} {noun}…")


async def run_agent_on_payloads(
    config: StarTriageConfig,
    payloads: list[dict[str, Any]],
    *,
    provider: Provider | None = None,
) -> str | None:
    """Run the agent over ``payloads`` and return the rendered markdown report.

    Returns the markdown string, or ``None`` when there is nothing to triage.
    Emitting the report (printing, writing a dated file, or appending to a
    triage markdown file) is left to the caller. When ``provider`` is omitted it
    is built from ``config`` (validating credentials, which may raise
    :class:`~startriage.config.AIConfigError`).
    """
    if not payloads:
        logger.info("No bugs to triage with the AI agent.")
        return None

    if provider is None:
        provider = build_provider(config.ai)

    system_prompt = load_system_prompt()
    spinner = _make_spinner(len(payloads))

    def on_progress(index: int, total: int, bug: str) -> None:
        if spinner is not None:
            label = f"LP #{bug}" if bug else "bug"
            spinner.set_status(f"Triaging {label} ({index}/{total})…")

    async with spinner if spinner is not None else contextlib.nullcontext():
        outcomes = await triage_bugs(provider, payloads, system_prompt, on_progress=on_progress)

    return render_report(outcomes)
