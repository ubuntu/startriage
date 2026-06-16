"""Tests for the triage report renderer (golden render + write fallback)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from startriage.ai import (
    AgentResult,
    BugOutcome,
    ProposedFix,
    append_report,
    render_report,
    report_filename,
    write_report,
)
from startriage.ai.render import AI_APPEND_NOTICE, _render_proposed_fix
from startriage.enums import ProposedFixKind, TriageStatus

_DAY = date(2026, 6, 15)


def _result(**overrides) -> AgentResult:
    base = {
        "bug": "123",
        "package": "pkg",
        "short_title": "boom on start",
        "status": TriageStatus.triaged,
        "tags": ["server-todo", "bitesize"],
        "analysis": "It crashes immediately.",
        "thought_process": "Read the logs, searched LP.",
        "proposed_fix": ProposedFix(kind=ProposedFixKind.reference, value="https://example.test/commit"),
        "references": ["https://bugs.launchpad.net/ubuntu/+bug/123"],
        "suggested_improvements": "Add a version cache.",
    }
    base.update(overrides)
    return AgentResult(**base)


def _outcome(result: AgentResult) -> BugOutcome:
    return BugOutcome(bug=result.bug, result=result, error=None, raw="{}")


# --- report_filename -------------------------------------------------------


def test_report_filename():
    assert report_filename(_DAY) == "autotriage-2026-06-15.md"


# --- proposed fix rendering ------------------------------------------------


def test_render_proposed_fix_none():
    fix = ProposedFix(kind=ProposedFixKind.none, value="")
    assert _render_proposed_fix(fix) == "_No fix proposed._"


def test_render_proposed_fix_reference():
    fix = ProposedFix(kind=ProposedFixKind.reference, value="https://x.test/c ")
    assert _render_proposed_fix(fix) == "https://x.test/c"


def test_render_proposed_fix_diff_is_fenced_not_applied():
    diff = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new"
    rendered = _render_proposed_fix(ProposedFix(kind=ProposedFixKind.diff, value=diff))
    assert rendered == f"```diff\n{diff}\n```"


def test_render_proposed_fix_empty_diff_falls_back():
    # A diff kind with no value should not emit an empty code block.
    fix = ProposedFix(kind=ProposedFixKind.diff, value="   ")
    assert _render_proposed_fix(fix) == "_No fix proposed._"


# --- full report -----------------------------------------------------------


def test_render_report_golden():
    outcomes = [_outcome(_result())]
    expected = (
        "# Automated triage — 2026-06-15\n"
        "\n"
        "## LP #123 — pkg — boom on start\n"
        "\n"
        "**Suggested status:** Triaged\n"
        "**Suggested tags:** server-todo, bitesize\n"
        "\n"
        "### Analysis\n"
        "\n"
        "It crashes immediately.\n"
        "\n"
        "### Thought Process\n"
        "\n"
        "Read the logs, searched LP.\n"
        "\n"
        "### Proposed Fix\n"
        "\n"
        "https://example.test/commit\n"
        "\n"
        "### References\n"
        "\n"
        "- https://bugs.launchpad.net/ubuntu/+bug/123\n"
        "\n"
        "## Suggested Improvements\n"
        "\n"
        "Add a version cache.\n"
    )
    assert render_report(outcomes, day=_DAY) == expected


def test_render_report_no_tags_and_no_references():
    result = _result(tags=[], references=[], suggested_improvements="")
    report = render_report([_outcome(result)], day=_DAY)
    assert "**Suggested tags:** _none_" in report
    assert "### References" not in report
    assert "## Suggested Improvements" not in report


def test_render_report_no_change_status():
    result = _result(status=TriageStatus.no_change)
    report = render_report([_outcome(result)], day=_DAY)
    assert "**Suggested status:** no-change" in report


def test_render_report_records_failures():
    ok = _outcome(_result(bug="123"))
    failed = BugOutcome(bug="456", result=None, error="agent output invalid", raw="junk")
    report = render_report([ok, failed], day=_DAY)

    assert "## LP #123 — pkg — boom on start" in report
    assert "## LP #456 — triage failed" in report
    assert "**Error:** agent output invalid" in report


def test_render_report_deduplicates_improvements():
    a = _outcome(_result(bug="1", suggested_improvements="Same note."))
    b = _outcome(_result(bug="2", suggested_improvements="Same note."))
    c = _outcome(_result(bug="3", suggested_improvements="Different note."))
    report = render_report([a, b, c], day=_DAY)

    # "Same note." appears once in the improvements section.
    improvements = report.split("## Suggested Improvements", 1)[1]
    assert improvements.count("Same note.") == 1
    assert "Different note." in improvements


# --- write_report ----------------------------------------------------------


def test_write_report_to_preferred_dir(tmp_path):
    path = write_report("content", day=_DAY, preferred_dir=tmp_path)
    assert path == tmp_path / "autotriage-2026-06-15.md"
    assert path.read_text() == "content"


def test_write_report_falls_back_to_snap_user_data(tmp_path, monkeypatch):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    snap = tmp_path / "snap"
    monkeypatch.setenv("SNAP_USER_DATA", str(snap))

    try:
        path = write_report("body", day=_DAY, preferred_dir=readonly)
    finally:
        readonly.chmod(0o700)

    assert path == snap / "autotriage-2026-06-15.md"
    assert path.read_text() == "body"


def test_write_report_reraises_without_snap(tmp_path, monkeypatch):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.delenv("SNAP_USER_DATA", raising=False)

    try:
        with_error = None
        try:
            write_report("body", day=_DAY, preferred_dir=readonly)
        except OSError as exc:
            with_error = exc
    finally:
        readonly.chmod(0o700)

    assert with_error is not None


def test_write_report_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write_report("x", day=_DAY)
    assert path == Path.cwd() / "autotriage-2026-06-15.md"
    assert path.read_text() == "x"


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
