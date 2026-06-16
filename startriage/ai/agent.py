"""Sequential agent loop: run one triage session per bug, skip-and-continue.

The provider (see :mod:`startriage.ai.provider`) runs the agent and returns its
final text; this module loads the behavioural system prompt, feeds each bug's
payload as the user message, and parses the result via the contract. A failure on
one bug is recorded and the run continues with the next, never aborting the batch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib.resources import files

from .contract import AgentResult, AgentResultError, parse_agent_result
from .provider import Provider

logger = logging.getLogger(__name__)


@dataclass
class BugOutcome:
    """Result of triaging a single bug: either a parsed result or a failure."""

    bug: str
    result: AgentResult | None
    error: str | None
    raw: str

    @property
    def ok(self) -> bool:
        return self.result is not None


def load_system_prompt() -> str:
    """Load the agent behavioural prompt shipped as a package resource."""
    prompt_path = files("startriage") / "data" / "agents_prompt.md"
    return prompt_path.read_text(encoding="utf-8")


def _log_outcome(outcome: BugOutcome) -> None:
    """Emit a per-bug step log: the decision at -v, deeper detail at -vv."""
    if outcome.ok and outcome.result is not None:
        result = outcome.result
        logger.info(
            "Bug %s → status=%s, tags=%s",
            outcome.bug,
            result.status.value,
            ", ".join(result.tags) or "(none)",
        )
        logger.debug("Bug %s proposed fix: %s", outcome.bug, result.proposed_fix.kind.value)
        if result.thought_process:
            logger.debug("Bug %s thought process: %s", outcome.bug, result.thought_process)
    else:
        logger.warning("Bug %s failed: %s", outcome.bug, outcome.error)


async def triage_bug(
    provider: Provider,
    payload: dict,
    system_prompt: str,
) -> BugOutcome:
    """Run one agent session for ``payload`` and parse its result.

    Never raises for triage/agent failures: any error is captured on the returned
    :class:`BugOutcome` so the caller can record it and continue.
    """
    bug = str(payload.get("number", ""))
    user_message = json.dumps(payload, ensure_ascii=False)
    logger.debug("Bug %s: sending %d-char payload to the agent", bug, len(user_message))
    try:
        raw = await provider.run(system_prompt, user_message)
    except Exception as exc:
        # Record any provider/runtime failure and keep going (skip-and-continue).
        return BugOutcome(bug=bug, result=None, error=f"provider error: {exc}", raw="")
    logger.debug("Bug %s: received %d-char agent response", bug, len(raw))
    try:
        result = parse_agent_result(raw)
    except AgentResultError as exc:
        return BugOutcome(bug=bug, result=None, error=str(exc), raw=raw)
    return BugOutcome(bug=bug, result=result, error=None, raw=raw)


async def triage_bugs(
    provider: Provider,
    payloads: list[dict],
    system_prompt: str | None = None,
) -> list[BugOutcome]:
    """Triage ``payloads`` sequentially, recording per-bug failures and continuing."""
    prompt = system_prompt if system_prompt is not None else load_system_prompt()
    total = len(payloads)
    outcomes: list[BugOutcome] = []
    for index, payload in enumerate(payloads, start=1):
        bug = str(payload.get("number", ""))
        logger.info("Triaging bug %s (%d/%d)…", bug, index, total)
        outcome = await triage_bug(provider, payload, prompt)
        _log_outcome(outcome)
        outcomes.append(outcome)
    succeeded = sum(o.ok for o in outcomes)
    logger.info("AI triage complete: %d succeeded, %d failed", succeeded, total - succeeded)
    return outcomes
