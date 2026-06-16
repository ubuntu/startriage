"""AI/agentic triage layer for startriage."""

from __future__ import annotations

from .agent import BugOutcome, load_system_prompt, triage_bug, triage_bugs
from .contract import (
    AgentResult,
    AgentResultError,
    ProposedFix,
    extract_json_block,
    parse_agent_result,
)
from .provider import (
    CopilotProvider,
    FakeProvider,
    Provider,
    build_client_kwargs,
    build_provider,
    build_session_kwargs,
)
from .render import render_report, report_filename, write_report

__all__ = [
    "AgentResult",
    "AgentResultError",
    "BugOutcome",
    "CopilotProvider",
    "FakeProvider",
    "ProposedFix",
    "Provider",
    "build_client_kwargs",
    "build_provider",
    "build_session_kwargs",
    "extract_json_block",
    "load_system_prompt",
    "parse_agent_result",
    "render_report",
    "report_filename",
    "triage_bug",
    "triage_bugs",
    "write_report",
]
