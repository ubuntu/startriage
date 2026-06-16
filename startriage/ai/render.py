"""Render triage results into the ``autotriage-YYYY-MM-DD.md`` report.

This is the tool side of the agent→tool contract: the agent only returns JSON, and
this module turns a batch of :class:`~startriage.ai.agent.BugOutcome` into markdown.
Proposed fixes are only *rendered* (a ``diff`` is shown in a fenced block, never
applied to any source tree), and per-bug failures are recorded so a skipped bug is
still visible in the report.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from ..enums import ProposedFixKind
from .agent import BugOutcome
from .contract import AgentResult, ProposedFix

#: Heading + notice prepended when an AI report is appended to a triage markdown
#: file, to keep the AI-generated content clearly separated from the human report.
AI_APPEND_NOTICE = (
    "---\n\n"
    "> **AI-generated triage aid.** The section below was produced by an automated "
    "agent. Review it critically — do **not** paste it into the official triage "
    "report verbatim.\n\n"
)


def report_filename(day: date | None = None) -> str:
    """Return the report file name for ``day`` (defaults to today)."""
    return f"autotriage-{(day or date.today()).isoformat()}.md"


def _render_proposed_fix(fix: ProposedFix) -> str:
    value = fix.value.strip()
    if fix.kind is ProposedFixKind.none or not value:
        return "_No fix proposed._"
    if fix.kind is ProposedFixKind.reference:
        return value
    # kind == diff: render only; the tool never applies it to a source tree.
    return f"```diff\n{value}\n```"


def _render_bug(result: AgentResult) -> str:
    package = result.package or "unknown"
    title = result.short_title or "(no title)"
    tags = ", ".join(result.tags) if result.tags else "_none_"

    lines = [
        f"## LP #{result.bug} — {package} — {title}",
        "",
        f"**Suggested status:** {result.status.value}",
        f"**Suggested tags:** {tags}",
        "",
        "### Analysis",
        "",
        result.analysis.strip() or "_No analysis provided._",
        "",
        "### Thought Process",
        "",
        result.thought_process.strip() or "_No thought process provided._",
        "",
        "### Proposed Fix",
        "",
        _render_proposed_fix(result.proposed_fix),
    ]
    if result.references:
        lines += ["", "### References", ""]
        lines += [f"- {ref}" for ref in result.references]
    return "\n".join(lines)


def _render_failure(outcome: BugOutcome) -> str:
    bug = outcome.bug or "(unknown)"
    return "\n".join(
        [
            f"## LP #{bug} — triage failed",
            "",
            f"**Error:** {outcome.error}",
        ]
    )


def _render_suggested_improvements(results: list[AgentResult]) -> str | None:
    """Aggregate non-empty, de-duplicated improvement notes across results."""
    seen: set[str] = set()
    blocks: list[str] = []
    for result in results:
        note = result.suggested_improvements.strip()
        if note and note not in seen:
            seen.add(note)
            blocks.append(note)
    if not blocks:
        return None
    return "\n\n".join(blocks)


def render_report(outcomes: list[BugOutcome], day: date | None = None) -> str:
    """Render a full markdown report for ``outcomes``.

    Successful results render as per-bug sections; failures are recorded inline.
    A trailing ``## Suggested Improvements`` section aggregates the agent's
    self-improvement notes when any were returned.
    """
    report_day = day or date.today()
    sections = [f"# Automated triage — {report_day.isoformat()}"]

    results = [o.result for o in outcomes if o.result is not None]

    for outcome in outcomes:
        if outcome.result is not None:
            sections.append(_render_bug(outcome.result))
        else:
            sections.append(_render_failure(outcome))

    improvements = _render_suggested_improvements(results)
    if improvements:
        sections.append(f"## Suggested Improvements\n\n{improvements}")

    return "\n\n".join(sections) + "\n"


def write_report(
    content: str,
    day: date | None = None,
    preferred_dir: Path | None = None,
) -> Path:
    """Write ``content`` to the report file, falling back to ``SNAP_USER_DATA``.

    Writes into ``preferred_dir`` (default: cwd). If that is not writable (e.g. a
    strict-snap read-only cwd), fall back to ``$SNAP_USER_DATA`` when set, otherwise
    re-raise the original error.
    """
    name = report_filename(day)
    target_dir = preferred_dir or Path.cwd()
    target = target_dir / name
    try:
        target.write_text(content, encoding="utf-8")
        return target
    except OSError:
        snap_data = os.environ.get("SNAP_USER_DATA")
        if not snap_data:
            raise
        fallback_dir = Path(snap_data)
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback = fallback_dir / name
        fallback.write_text(content, encoding="utf-8")
        return fallback


def append_report(path: Path, content: str) -> Path:
    """Append an AI ``content`` report to an existing markdown file at ``path``.

    A horizontal rule and a notice (:data:`AI_APPEND_NOTICE`) are inserted first so
    the AI-generated section is clearly separated from the human-written triage
    report and is not mistaken for part of it.
    """
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n" + AI_APPEND_NOTICE + content)
    return path
