"""Sequential agent loop: run one triage session per bug, skip-and-continue.

The provider (see :mod:`startriage.ai.provider`) runs the agent and returns its
final text; this module loads the behavioural system prompt, feeds each bug's
payload as the user message, and parses the result via the contract. A failure on
one bug is recorded and the run continues with the next, never aborting the batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files

from .contract import AgentResult, AgentResultError, parse_agent_result
from .provider import Provider


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
    try:
        raw = await provider.run(system_prompt, user_message)
    except Exception as exc:
        # Record any provider/runtime failure and keep going (skip-and-continue).
        return BugOutcome(bug=bug, result=None, error=f"provider error: {exc}", raw="")
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
    outcomes: list[BugOutcome] = []
    for payload in payloads:
        outcomes.append(await triage_bug(provider, payload, prompt))
    return outcomes
