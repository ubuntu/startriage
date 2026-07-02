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
from .render import (
    append_report,
    render_bug_metadata,
    render_report,
    report_filename,
    resolve_report_dir,
    write_report,
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
    "AgentResultError",
    "BugOutcome",
    "CopilotProvider",
    "FakeProvider",
    "ProposedFix",
    "Provider",
    "append_report",
    "build_client_kwargs",
    "build_provider",
    "build_session_kwargs",
    "describe_bug_specs",
    "extract_json_block",
    "gather_user_bug_payloads",
    "load_system_prompt",
    "parse_agent_result",
    "parse_bug_number",
    "payloads_from_tasks",
    "render_bug_metadata",
    "render_report",
    "report_filename",
    "resolve_report_dir",
    "run_agent_on_payloads",
    "run_ai_over_bug_specs",
    "run_ai_over_triage_results",
    "triage_bug",
    "triage_bugs",
    "write_report",
]
