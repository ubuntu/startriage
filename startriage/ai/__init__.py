"""AI/agentic triage layer for startriage."""

from __future__ import annotations

from .agent import BugOutcome, run_phase
from .contract import AgentResult, ProposedFix
from .pipeline import Pipeline, triage_bugs, triage_one
from .provider import (
    CopilotProvider,
    FakeProvider,
    FakeTurn,
    Provider,
    SessionEvent,
    SessionEventKind,
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
    AITriageBatch,
    RunOptions,
    analyze_bug_specs,
    analyze_triage_results,
    analyze_work_items,
    describe_bug_specs,
    gather_user_bug_payloads,
    parse_bug_number,
    payloads_from_tasks,
    run_agent_on_payloads,
    run_ai_over_bug_specs,
    run_ai_over_triage_results,
    work_item_from_payload,
    work_items_from_tasks,
)

__all__ = [
    "AITriageBatch",
    "AgentResult",
    "BugOutcome",
    "CopilotProvider",
    "FakeProvider",
    "FakeTurn",
    "Pipeline",
    "ProposedFix",
    "Provider",
    "RunOptions",
    "SessionEvent",
    "SessionEventKind",
    "SessionResult",
    "SessionSpec",
    "analyze_bug_specs",
    "analyze_triage_results",
    "analyze_work_items",
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
    "triage_one",
    "work_item_from_payload",
    "work_items_from_tasks",
]
