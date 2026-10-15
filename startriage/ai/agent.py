"""Run one phase session, and the per-bug outcome the pipeline builds.

A phase is one session with one skill and one ``submit_<phase>`` tool. A
failure never raises: it is recorded on the :class:`PhaseOutcome` so the
pipeline can continue where it can.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .contract import AgentResult
from .phases import PhaseOutcome, PhaseState
from .provider import Provider, SessionSpec
from .supervisor import MAX_CALLS
from .tools import Submission, ToolSpec

logger = logging.getLogger(__name__)

NUDGE = "You have not called {submit} yet. Call it now with your result; do not answer in text."


@dataclass
class BugOutcome:
    """Result of triaging a single bug."""

    bug: str
    result: AgentResult | None = None
    error: str | None = None
    route: str = ""
    # Why the bug was not sent to the model (denied / skip routes).
    note: str = ""
    phases: list[PhaseOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result is not None

    @property
    def not_sent(self) -> bool:
        return self.result is None and self.error is None


async def run_phase(
    provider: Provider,
    *,
    phase: str,
    bug: str,
    system_prompt: str,
    task: str,
    tools: Sequence[ToolSpec],
    submission: Submission,
    skill_sha256: str = "",
    working_directory: Path | None = None,
    max_calls: int = MAX_CALLS,
) -> PhaseOutcome:
    """Run one phase session and return what it submitted (or why it failed)."""
    spec = SessionSpec(
        system_prompt=system_prompt,
        task=task,
        tools=[*tools, submission.tool()],
        working_directory=working_directory,
        max_calls=max_calls,
        nudge=NUDGE.format(submit=submission.name),
        is_done=lambda: submission.result is not None,
    )
    logger.debug("Bug %s %s: %d-char prompt, %d-char task", bug, phase, len(system_prompt), len(task))
    outcome = PhaseOutcome(phase=phase, state=PhaseState.failed, sessions=1, skill_sha256=skill_sha256)
    try:
        session = await provider.run(spec)
    except Exception as exc:
        logger.warning("Bug %s %s: provider run failed", bug, phase, exc_info=True)
        outcome.summary = f"provider error: {exc}"
        return outcome
    outcome.tool_calls = list(session.tool_calls)
    outcome.input_tokens = session.input_tokens
    outcome.output_tokens = session.output_tokens
    logger.info(
        "Bug %s %s: %d tool calls (%s), %d input / %d output tokens",
        bug,
        phase,
        len(session.tool_calls),
        ", ".join(session.tool_calls) or "none",
        session.input_tokens,
        session.output_tokens,
    )
    if submission.result is None:
        if session.aborted:
            outcome.summary = "the agent kept repeating a tool call and was aborted"
        else:
            outcome.summary = f"the agent did not call {submission.name}"
        return outcome
    outcome.state = PhaseState.done
    outcome.result = submission.result
    outcome.feedback = submission.feedback
    return outcome
