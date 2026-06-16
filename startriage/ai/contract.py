"""Agent → tool result contract: the JSON each bug triage must return.

The Copilot CLI returns a free-text final assistant message, so the agent is
instructed to end with a single fenced ``json`` block. This module extracts that
block, parses it, and validates it against the schema in ``agents_prompt.md``.
Validation is enforced in code (status / fix-kind enums) so a hallucinated or
malformed result is rejected rather than trusted.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, ValidationError

from ..enums import ProposedFixKind, TriageStatus

# Matches fenced code blocks, optionally tagged with a language (e.g. ```json).
_FENCED_BLOCK = re.compile(
    r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)\r?\n```",
    re.DOTALL,
)


class AgentResultError(ValueError):
    """Raised when the agent's output cannot be parsed/validated as a result."""


class ProposedFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProposedFixKind
    value: str = ""


class AgentResult(BaseModel):
    """One bug's triage result, as returned by the agent and rendered by the tool."""

    # Tolerate extra keys: LLM output is noisy and harmless additions should not
    # fail an otherwise-valid result. The fields below are still validated strictly.
    model_config = ConfigDict(extra="ignore")

    bug: str
    package: str = ""
    short_title: str = ""
    status: TriageStatus
    tags: list[str] = []
    analysis: str = ""
    thought_process: str = ""
    proposed_fix: ProposedFix
    references: list[str] = []
    suggested_improvements: str = ""


def extract_json_block(text: str) -> str:
    """Return the JSON payload of the last fenced block in ``text``.

    Prefers a ```json-tagged block; falls back to the last untagged fenced block so
    a missing language hint does not break parsing. Raises :class:`AgentResultError`
    when no fenced block is present.
    """
    matches = _FENCED_BLOCK.findall(text)
    if not matches:
        raise AgentResultError("no fenced code block found in agent output")

    json_blocks = [body for lang, body in matches if lang.lower() == "json"]
    if json_blocks:
        return json_blocks[-1].strip()
    # No language-tagged json block; use the last fenced block of any kind.
    return matches[-1][1].strip()


def parse_agent_result(text: str) -> AgentResult:
    """Extract, decode, and validate a single :class:`AgentResult` from agent text.

    Raises :class:`AgentResultError` on a missing block, invalid JSON, or schema /
    enum validation failure.
    """
    block = extract_json_block(text)
    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise AgentResultError(f"agent output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentResultError("agent JSON result must be an object")
    try:
        return AgentResult.model_validate(data)
    except ValidationError as exc:
        raise AgentResultError(f"agent result failed validation: {exc}") from exc
