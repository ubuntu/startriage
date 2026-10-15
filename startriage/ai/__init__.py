"""AI/agentic triage layer for startriage."""

from __future__ import annotations

from .agent import BugOutcome, run_phase
from .contract import AgentResult, ProposedFix
from .pipeline import Pipeline, triage_bugs
from .provider import (
    CopilotProvider,
    FakeProvider,
    FakeTurn,
    Provider,
    SessionResult,
    SessionSpec,
    build_client_kwargs,
    build_provider,
    build_session_kwargs,
)
from .render import (
    append_report,
    emit_ai_report,
    render_bug_metadata,
    render_report,
)
from .run import (
    describe_bug_specs,
    gather_user_bug_payloads,
    parse_bug_number,
    payloads_from_tasks,
    run_agent_on_payloads,
    run_ai_over_bug_specs,
    run_ai_over_triage_results,
)

__all__ = [
    "AgentResult",
    "BugOutcome",
    "CopilotProvider",
    "FakeProvider",
    "FakeTurn",
    "Pipeline",
    "ProposedFix",
    "Provider",
    "SessionResult",
    "SessionSpec",
    "append_report",
    "build_client_kwargs",
    "build_provider",
    "build_session_kwargs",
    "describe_bug_specs",
    "emit_ai_report",
    "gather_user_bug_payloads",
    "parse_bug_number",
    "payloads_from_tasks",
    "render_bug_metadata",
    "render_report",
    "run_agent_on_payloads",
    "run_ai_over_bug_specs",
    "run_ai_over_triage_results",
    "run_phase",
    "triage_bugs",
]
