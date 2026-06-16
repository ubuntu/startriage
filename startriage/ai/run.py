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

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import StarTriageConfig
from .agent import load_system_prompt, triage_bugs
from .provider import Provider, build_provider
from .render import render_report, write_report

if TYPE_CHECKING:
    from ..sources.launchpad.models import Task

logger = logging.getLogger(__name__)

_BUG_DIGITS = re.compile(r"\d+")


def parse_bug_number(spec: str) -> str:
    """Extract a Launchpad bug number from a URL, ``#NNNNNN`` or bare ``NNNNNN``.

    The last run of digits wins, so package names containing digits in a full
    ``.../+source/<pkg>/+bug/<n>`` URL do not confuse the parse.
    """
    matches = _BUG_DIGITS.findall(spec)
    if not matches:
        raise ValueError(f"could not parse a Launchpad bug number from {spec!r}")
    return matches[-1]


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


async def run_agent_on_payloads(
    config: StarTriageConfig,
    payloads: list[dict[str, Any]],
    *,
    provider: Provider | None = None,
    preferred_dir: Path | None = None,
) -> Path | None:
    """Run the agent over ``payloads`` and write the dated report.

    Returns the report path, or ``None`` when there is nothing to triage. When
    ``provider`` is omitted it is built from ``config`` (validating credentials,
    which may raise :class:`~startriage.config.AIConfigError`).
    """
    if not payloads:
        logger.info("No bugs to triage with the AI agent.")
        return None

    if provider is None:
        provider = build_provider(config.ai)

    system_prompt = load_system_prompt()
    outcomes = await triage_bugs(provider, payloads, system_prompt)
    report = render_report(outcomes)
    path = write_report(report, preferred_dir=preferred_dir)
    logger.info("AI triage report written to %s", path)
    return path
