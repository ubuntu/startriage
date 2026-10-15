"""Tests for the triage report renderer (golden render + append)."""

from __future__ import annotations

from datetime import date

from startriage.ai import (
    AgentResult,
    BugOutcome,
    ProposedFix,
    append_report,
    render_bug_metadata,
    render_report,
)
from startriage.ai.contract import Importance, ProposedFixKind, SRUCandidate, TriageStatus
from startriage.ai.phases import PhaseOutcome, PhaseState
from startriage.ai.render import AI_APPEND_NOTICE, SRU_NOTICE, _render_proposed_fix

_DAY = date(2026, 6, 15)


def _result(**overrides) -> AgentResult:
    base = {
        "bug": "123",
        "package": "pkg",
        "short_title": "boom on start",
        "status": TriageStatus.triaged,
        "importance": Importance.high,
        "series": {"noble": TriageStatus.triaged},
        "tags": ["server-todo"],
        "analysis": "It crashes immediately.",
        "suggested_comment": "Thanks.\n\nFixed upstream.",
        "proposed_fix": ProposedFix(kind=ProposedFixKind.reference, value="https://example.test/commit"),
        "references": ["https://bugs.launchpad.net/ubuntu/+bug/123"],
        "sru_candidates": [SRUCandidate(series="noble", reason="fixed in 2.0 upstream")],
    }
    base.update(overrides)
    return AgentResult.model_validate(base)


def _outcome(result: AgentResult) -> BugOutcome:
    return BugOutcome(bug=result.bug, result=result)


# --- bug metadata rendering ------------------------------------------------


def test_render_bug_metadata_renders_key_fields():
    payload = {
        "number": "2101234",
        "url": "https://bugs.launchpad.net/ubuntu/+bug/2101234",
        "short_title": "boom on start",
        "status": "Confirmed",
        "importance": "High",
        "heat": 42,
        "tags": ["server-todo", "bitesize"],
        "duplicate_of": "999",
        "affected": [{"target": "pkg (Ubuntu)", "status": "Confirmed", "importance": "High"}],
        "description": "It crashes immediately.",
        "attachments": [{"title": "fix.patch", "type": "text/plain", "is_patch": True}],
        "comments": [{"author": "alice", "date": "2026-06-15", "text": "Seeing this too."}],
    }
    rendered = render_bug_metadata([payload])
    assert "# Bug metadata" in rendered
    assert "## LP #2101234 — boom on start" in rendered
    assert "**Status:** Confirmed" in rendered
    assert "**Tags:** server-todo, bitesize" in rendered
    assert "**Duplicate of:** LP #999" in rendered
    assert "- pkg (Ubuntu) — Confirmed (High)" in rendered
    assert "It crashes immediately." in rendered
    assert "- fix.patch (text/plain) [patch]" in rendered
    assert "**alice** — 2026-06-15" in rendered


# --- proposed fix rendering ------------------------------------------------


def test_render_proposed_fix_diff_is_fenced_and_an_empty_one_is_no_fix():
    diff = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new"
    rendered = _render_proposed_fix(ProposedFix(kind=ProposedFixKind.diff, value=diff))
    assert rendered == f"```diff\n{diff}\n```"
    fix = ProposedFix(kind=ProposedFixKind.diff, value="   ")
    assert _render_proposed_fix(fix) == "_No fix proposed._"


def test_render_report_lists_host_patch_checks():
    result = _result(fix_applies={"noble": "clean", "devel": "already applied"})
    rendered = render_report([_outcome(result)], _DAY)
    assert "Checked by the host (patches applied):\n\n- noble: clean\n- devel: already applied" in rendered
    assert "Checked by the host" not in render_report([_outcome(_result())], _DAY)


# --- full report -----------------------------------------------------------


def _phase(phase: str, state=PhaseState.done, summary: str = "", **kwargs) -> PhaseOutcome:
    return PhaseOutcome(phase=phase, state=state, summary=summary, **kwargs)


def test_render_report_golden():
    outcome = _outcome(_result())
    outcome.route = "new"
    outcome.phases = [
        _phase("precheck", summary="route=new: no team activity"),
        _phase(
            "validate-report",
            summary="verdict=bug: a crash",
            tool_calls=["lp_get_bug", "lp_get_bug", "submit_validation"],
            input_tokens=1200,
            output_tokens=80,
            sessions=1,
        ),
    ]
    expected = (
        "# Automated triage — 2026-06-15\n"
        "\n"
        "## LP #123 — pkg — boom on start\n"
        "\n"
        "**Route:** new\n"
        "**Suggested status:** Triaged\n"
        "**Suggested importance:** High\n"
        "**Per series:** noble: Triaged\n"
        "**Suggested tags:** server-todo\n"
        "\n"
        "### Analysis\n"
        "\n"
        "It crashes immediately.\n"
        "\n"
        "### Suggested Comment\n"
        "\n"
        "> Thanks.\n"
        ">\n"
        "> Fixed upstream.\n"
        "\n"
        "### Proposed Fix\n"
        "\n"
        "https://example.test/commit\n"
        "\n"
        "### References\n"
        "\n"
        "- https://bugs.launchpad.net/ubuntu/+bug/123\n"
        "\n"
        "### SRU Candidates\n"
        "\n"
        "- noble: fixed in 2.0 upstream\n"
        "\n"
        f"_{SRU_NOTICE}_\n"
        "\n"
        "### Thought Process\n"
        "\n"
        "- **precheck** (done): route=new: no team activity\n"
        "- **validate-report** (done): verdict=bug: a crash"
        " _(tools: lp_get_bug x2, submit_validation; 1,200 in / 80 out tokens)_\n"
        "\n"
        "## Cost\n"
        "\n"
        "| Phase | Sessions | Input tokens | Output tokens |\n"
        "|---|---:|---:|---:|\n"
        "| validate-report | 1 | 1,200 | 80 |\n"
        "| **total** | 1 | 1,200 | 80 |\n"
    )
    assert render_report([outcome], day=_DAY) == expected


def test_render_report_leaves_out_empty_sections():
    result = _result(
        tags=[],
        references=[],
        sru_candidates=[],
        suggested_comment="",
        status=TriageStatus.no_change,
        importance=Importance.undecided,
    )
    report = render_report([_outcome(result)], day=_DAY)
    assert "**Suggested tags:** _none_" in report
    assert "**Suggested status:** no-change" in report
    assert "importance" not in report
    assert "### References" not in report
    assert "### SRU Candidates" not in report
    assert "### Suggested Comment" not in report
    assert "## Cost" not in report


def test_render_report_records_failures_and_bugs_not_sent():
    ok = _outcome(_result(bug="123"))
    failed = BugOutcome(bug="456", error="validate-report failed: provider error: boom")
    failed.phases = [_phase("validate-report", PhaseState.failed, "provider error: boom")]
    denied = BugOutcome(bug="789", route="denied", note="Private bug: not sent to the AI.")
    denied.phases = [_phase("precheck", summary="route=denied: private (Private Security)")]
    report = render_report([ok, failed, denied], day=_DAY)

    assert "## LP #123 — pkg — boom on start" in report
    assert "## LP #456 — triage failed" in report
    assert "**Error:** validate-report failed: provider error: boom" in report
    assert "- **validate-report** (failed): provider error: boom" in report
    assert "## LP #789 — not triaged by AI\n\nPrivate bug: not sent to the AI." in report
    assert "_Precheck: route=denied: private (Private Security)_" in report


def test_render_report_groups_skill_feedback():
    a = _outcome(_result(bug="1"))
    a.phases = [_phase("validate-report", feedback="Explain\nsync requests.")]
    b = _outcome(_result(bug="2"))
    b.phases = [_phase("validate-report", feedback="Too long."), _phase("write-report", feedback="Fine.")]
    report = render_report([a, b], day=_DAY)
    feedback = report.split("## Skill Feedback", 1)[1]
    assert "### validate-report\n\n- LP #1: Explain sync requests.\n- LP #2: Too long." in feedback
    assert "### write-report\n\n- LP #2: Fine." in feedback


# --- append_report ---------------------------------------------------------


def test_append_report_adds_notice_after_existing_content(tmp_path):
    md = tmp_path / "triage.md"
    md.write_text("# Triage\n\nSome human content.\n")

    returned = append_report(md, "# Automated triage — 2026-06-15\n\n## LP #1\n")

    assert returned == md
    text = md.read_text()
    # Original content is preserved and comes first.
    assert text.startswith("# Triage\n\nSome human content.\n")
    # A notice separates the AI section from the human report.
    assert AI_APPEND_NOTICE in text
    assert text.index("Some human content.") < text.index("Automated triage")
    assert text.endswith("## LP #1\n")
