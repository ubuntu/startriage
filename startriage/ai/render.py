"""Render triage results into markdown.

This is the tool side of the agent→tool contract: the agent submits a result, and
this module turns a batch of :class:`~startriage.ai.agent.BugOutcome` into markdown.
Proposed fixes are only *rendered* (a ``diff`` is shown in a fenced block, never
applied to any source tree), and per-bug failures are recorded so a skipped bug is
still visible in the report. The rendered markdown is returned to the caller, which
decides how to emit it (print to stdout or append to a triage markdown file).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from .agent import BugOutcome
from .contract import AgentResult, Importance, ProposedFix, ProposedFixKind
from .phases import PhaseOutcome
from .pipeline import phase_totals

SRU_NOTICE = "SRU feasibility not assessed; at the triager's discretion."

#: Heading + notice prepended when an AI report is appended to a triage markdown
#: file, to keep the AI-generated content clearly separated from the human report.
AI_APPEND_NOTICE = (
    "---\n\n"
    "> **AI-generated triage aid.** The section below was produced by an automated "
    "agent. Review it critically — do **not** paste it into the official triage "
    "report verbatim.\n\n"
)


def _render_proposed_fix(fix: ProposedFix) -> str:
    value = fix.value.strip()
    if fix.kind is ProposedFixKind.none or not value:
        return "_No fix proposed._"
    if fix.kind is ProposedFixKind.reference:
        return value
    # kind == diff: rendered as is; the host only dry-runs it (fix_applies).
    return f"```diff\n{value}\n```"


def _render_tool_calls(calls: list[str]) -> str:
    counts = Counter(calls)
    return ", ".join(f"{name} x{n}" if n > 1 else name for name, n in counts.items())


def _render_phases(phases: list[PhaseOutcome]) -> str:
    """The thought process: what each phase did, from the host's log (not the model's claims)."""
    if not phases:
        return "_No phases ran._"
    lines = []
    for phase in phases:
        line = f"- **{phase.phase}** ({phase.state.value})"
        if phase.summary:
            line += f": {' '.join(phase.summary.split())}"
        details = []
        if phase.tool_calls:
            details.append(f"tools: {_render_tool_calls(phase.tool_calls)}")
        if phase.sessions:
            details.append(f"{phase.input_tokens:,} in / {phase.output_tokens:,} out tokens")
        if details:
            line += f" _({'; '.join(details)})_"
        lines.append(line)
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _render_bug(outcome: BugOutcome, result: AgentResult) -> str:
    package = result.package or "unknown"
    title = result.short_title or "(no title)"
    tags = ", ".join(result.tags) if result.tags else "_none_"

    lines = [f"## LP #{result.bug} — {package} — {title}", ""]
    if outcome.route:
        lines.append(f"**Route:** {outcome.route}")
    lines.append(f"**Suggested status:** {result.status.value}")
    if result.importance is not Importance.undecided:
        lines.append(f"**Suggested importance:** {result.importance.value}")
    if result.series:
        per_series = ", ".join(f"{name}: {status.value}" for name, status in result.series.items())
        lines.append(f"**Per series:** {per_series}")
    lines.append(f"**Suggested tags:** {tags}")
    if result.duplicate_of:
        lines.append(f"**Duplicate of:** LP #{result.duplicate_of.lstrip('#')}")
    lines += ["", "### Analysis", "", result.analysis.strip() or "_No analysis provided._"]
    if result.suggested_comment.strip():
        lines += ["", "### Suggested Comment", "", _quote(result.suggested_comment)]
    lines += ["", "### Proposed Fix", "", _render_proposed_fix(result.proposed_fix)]
    if result.fix_applies:
        lines += ["", "Checked by the host (patches applied):", ""]
        lines += [f"- {ref}: {state}" for ref, state in result.fix_applies.items()]
    if result.references:
        lines += ["", "### References", ""]
        lines += [f"- {ref}" for ref in result.references]
    if result.sru_candidates:
        lines += ["", "### SRU Candidates", ""]
        lines += [f"- {c.series}: {c.reason}" for c in result.sru_candidates]
        lines += ["", f"_{SRU_NOTICE}_"]
    if result.upstream_suggestion.strip():
        lines += ["", "### Upstream", "", result.upstream_suggestion.strip()]
    lines += ["", "### Thought Process", "", _render_phases(outcome.phases)]
    return "\n".join(lines)


def _render_not_sent(outcome: BugOutcome) -> str:
    reason = outcome.phases[0].summary if outcome.phases else outcome.route
    return "\n".join(
        [
            f"## LP #{outcome.bug or '(unknown)'} — not triaged by AI",
            "",
            outcome.note,
            "",
            f"_Precheck: {reason}_",
        ]
    )


def _render_failure(outcome: BugOutcome) -> str:
    bug = outcome.bug or "(unknown)"
    lines = [f"## LP #{bug} — triage failed", "", f"**Error:** {outcome.error}"]
    if outcome.phases:
        lines += ["", "### Thought Process", "", _render_phases(outcome.phases)]
    return "\n".join(lines)


def _render_feedback(outcomes: list[BugOutcome]) -> str | None:
    """Skill feedback grouped per skill."""
    by_skill: dict[str, list[str]] = defaultdict(list)
    for outcome in outcomes:
        for phase in outcome.phases:
            if phase.feedback:
                by_skill[phase.phase].append(f"- LP #{outcome.bug}: {' '.join(phase.feedback.split())}")
    if not by_skill:
        return None
    blocks = [f"### {skill}\n\n" + "\n".join(items) for skill, items in by_skill.items()]
    return "## Skill Feedback\n\n" + "\n\n".join(blocks)


def _render_cost(outcomes: list[BugOutcome]) -> str | None:
    totals = phase_totals(outcomes)
    if not totals:
        return None
    rows = [f"| {name} | {s} | {i:,} | {o:,} |" for name, (s, i, o) in totals.items()]
    sessions = sum(s for s, _, _ in totals.values())
    tokens_in = sum(i for _, i, _ in totals.values())
    tokens_out = sum(o for _, _, o in totals.values())
    rows.append(f"| **total** | {sessions} | {tokens_in:,} | {tokens_out:,} |")
    header = ["## Cost", "", "| Phase | Sessions | Input tokens | Output tokens |", "|---|---:|---:|---:|"]
    return "\n".join(header + rows)


def render_report(outcomes: list[BugOutcome], day: date | None = None, tools: list[str] | None = None) -> str:
    """Render a full markdown report for ``outcomes``.

    Results render as per-bug sections; bugs not sent to the AI and failures are
    recorded inline. Skill feedback (when enabled) and the cost per phase close
    the report. ``tools`` lists the tools the agent could use.
    """
    report_day = day or date.today()
    sections = [f"# Automated triage — {report_day.isoformat()}"]
    if tools is not None:
        sections.append(f"_Agent tools: {', '.join(tools) or 'none'}_")

    for outcome in outcomes:
        if outcome.result is not None:
            sections.append(_render_bug(outcome, outcome.result))
        elif outcome.error is None:
            sections.append(_render_not_sent(outcome))
        else:
            sections.append(_render_failure(outcome))

    for extra in (_render_feedback(outcomes), _render_cost(outcomes)):
        if extra:
            sections.append(extra)

    return "\n\n".join(sections) + "\n"


def _render_bug_metadata(payload: dict[str, Any]) -> str:
    """Render one bug payload as human-readable metadata (no AI analysis)."""
    number = payload.get("number") or "(unknown)"
    title = payload.get("short_title") or payload.get("title") or "(no title)"
    tags = payload.get("tags") or []

    lines = [f"## LP #{number} — {title}", ""]
    if payload.get("url"):
        lines.append(f"**URL:** {payload['url']}")
    lines.append(f"**Status:** {payload.get('status') or 'unknown'}")
    lines.append(f"**Importance:** {payload.get('importance') or 'unknown'}")
    if payload.get("heat") is not None:
        lines.append(f"**Heat:** {payload['heat']}")
    lines.append(f"**Tags:** {', '.join(tags) if tags else '_none_'}")
    if payload.get("duplicate_of"):
        lines.append(f"**Duplicate of:** LP #{payload['duplicate_of']}")

    affected = payload.get("affected") or []
    if affected:
        lines += ["", "### Affected", ""]
        for target in affected:
            name = target.get("target") or "(unknown target)"
            status = target.get("status") or "?"
            importance = target.get("importance") or "?"
            lines.append(f"- {name} — {status} ({importance})")

    description = (payload.get("description") or "").strip()
    lines += ["", "### Description", "", description or "_No description._"]

    attachments = payload.get("attachments") or []
    if attachments:
        lines += ["", "### Attachments", ""]
        for att in attachments:
            title_text = att.get("title") or "(untitled)"
            suffix = " [patch]" if att.get("is_patch") else ""
            lines.append(f"- {title_text} ({att.get('type') or 'unknown'}){suffix}")

    comments = payload.get("comments") or []
    if comments:
        lines += ["", f"### Comments ({len(comments)})"]
        for comment in comments:
            author = comment.get("author") or "unknown"
            when = comment.get("date") or "unknown date"
            text = (comment.get("text") or "").strip()
            lines += ["", f"**{author}** — {when}", "", text or "_(empty)_"]

    return "\n".join(lines)


def render_bug_metadata(payloads: list[dict[str, Any]]) -> str:
    """Render bug payloads as human-readable metadata, without any AI analysis.

    This is what ``analyze`` prints when ``--ai`` is not given: the raw bug
    context (status, tags, affected targets, description, comments) as gathered
    for the agent, so a human can eyeball it before deciding to run the agent.
    """
    sections = ["# Bug metadata"]
    sections += [_render_bug_metadata(payload) for payload in payloads]
    return "\n\n".join(sections) + "\n"


def append_report(path: Path, content: str) -> Path:
    """Append an AI ``content`` report to an existing markdown file at ``path``.

    A horizontal rule and a notice (:data:`AI_APPEND_NOTICE`) are inserted first so
    the AI-generated section is clearly separated from the human-written triage
    report and is not mistaken for part of it.
    """
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n" + AI_APPEND_NOTICE + content)
    return path


def emit_ai_report(report: str, markdown_path: Path | None) -> None:
    """Emit an AI ``report`` for a ``triage --ai`` run.

    With ``--markdown`` the AI section is appended (behind :data:`AI_APPEND_NOTICE`)
    to that same file, so the human triage report and the AI aid live in one
    cohesive document. Without ``--markdown`` the report is printed to stdout,
    together with the normal triage output that already went there.
    """
    if markdown_path is not None:
        append_report(markdown_path, report)
        print(f"AI triage appended to {markdown_path}")
    else:
        print("\n" + AI_APPEND_NOTICE + report)
