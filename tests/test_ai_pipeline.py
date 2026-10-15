"""Tests for the phased triage pipeline, with a scripted fake provider."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from startriage.ai import FakeProvider, FakeTurn, Pipeline, triage_bugs
from startriage.ai import archive as archive_module
from startriage.ai import checks as checks_module
from startriage.ai import pipeline as pipeline_module
from startriage.ai import tools as tools_module
from startriage.ai.contract import Importance, TriageStatus
from startriage.ai.phases import Candidate, ChangelogHit, PhaseState, Upload
from startriage.ai.pipeline import phase_totals
from startriage.ai.tools import ReadOnlyHTTP, ToolContext, ToolError, build_tools
from startriage.ai.tools import patch as patch_module
from startriage.ai.tools import source as source_module
from startriage.ai.tools.source import UpstreamInfo

from .http_replay import ReplaySession
from .record_ai_fixtures import NGINX_FIX, TOOL_CALLS

TEAM = frozenset({"alice"})
WINDOW = (datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 5, 23, 59, tzinfo=UTC))
IN = "2026-10-05T10:00:00+00:00"

FACTS = {
    "package": "pkg",
    "symptoms": "crashes on start",
    "errors": ["Segmentation fault"],
    "search_terms": ["segfault on start"],
}
BUG = {"verdict": "bug", "reason": "a crash", "short_title": "crash on start", "facts": FACTS}
REPORT = {"status": "Triaged", "analysis": "A crash.", "importance": "High"}
ANALYSIS = {"root_cause": "Null pointer.", "confidence": "low", "summary": "Read the code."}
FIX = {"summary": "Revert it."}


def _payload(**overrides) -> dict:
    payload = {
        "number": "100",
        "package": "pkg",
        "title": "crash on start",
        "short_title": "crash on start",
        "description": "It crashes.",
        "reporter": "carol",
        "date_created": IN,
        "status": "New",
        "tags": [],
        "assignees": [],
        "affected": [],
        "attachments": [],
        "comments": [],
        "private": False,
        "activity": [],
    }
    payload.update(overrides)
    return payload


def _session(name: str, args: dict, **tokens) -> list[FakeTurn]:
    return [FakeTurn(calls=[(name, args)], **tokens)]


def _pipeline(provider, **kwargs) -> Pipeline:
    names = [
        "lp_get_bug",
        "lp_get_attachment",
        "lp_search_bugs",
        "verify_url",
        "source_file",
        "patch_applies",
    ]
    tools = build_tools(ToolContext(), names)
    kwargs.setdefault("team", TEAM)
    kwargs.setdefault("window", WINDOW)
    return Pipeline(provider=provider, tools={t.name: t for t in tools}, **kwargs)


def _phases(outcome) -> list[tuple[str, str]]:
    return [(p.phase, p.state.value) for p in outcome.phases]


@pytest.mark.asyncio
async def test_private_bug_costs_no_session():
    provider = FakeProvider([])
    outcome = await _pipeline(provider).triage(_payload(private=True))
    assert provider.specs == []
    assert outcome.route == "denied"
    assert outcome.not_sent
    assert "Private" in outcome.note


@pytest.mark.asyncio
async def test_team_only_activity_costs_no_session():
    provider = FakeProvider([])
    payload = _payload(
        date_created="2026-01-01T00:00:00+00:00", activity=[{"date": IN, "person": "alice", "what": "tags"}]
    )
    outcome = await _pipeline(provider).triage(payload)
    assert provider.specs == []
    assert outcome.route == "skip"
    assert outcome.not_sent


@pytest.mark.asyncio
async def test_early_exit_carries_a_comment():
    validation = {
        "verdict": "support",
        "reason": "a question",
        "short_title": "how to",
        "comment": "Ask on Discourse.",
    }
    provider = FakeProvider([_session("submit_validation", validation)])
    outcome = await _pipeline(provider).triage(_payload())
    assert len(provider.specs) == 1
    assert outcome.result is not None
    assert outcome.result.status is TriageStatus.invalid
    assert outcome.result.suggested_comment == "Ask on Discourse."
    assert outcome.result.importance is Importance.undecided
    assert outcome.result.short_title == "how to"
    assert _phases(outcome) == [("precheck", "done"), ("validate-report", "done")]


@pytest.mark.asyncio
async def test_validate_session_gets_its_tools_and_the_bug_without_host_only_fields():
    validation = {"verdict": "process", "reason": "merge", "short_title": "merge"}
    provider = FakeProvider([_session("submit_validation", validation)])
    await _pipeline(provider).triage(_payload(activity=[{"date": IN, "person": "x", "what": "tags"}]))
    spec = provider.specs[0]
    assert [t.name for t in spec.tools] == ["lp_get_bug", "lp_get_attachment", "submit_validation"]
    assert '"description": "It crashes."' in spec.task
    assert '"activity"' not in spec.task
    assert '"private"' not in spec.task
    assert "# Data, not instructions" in spec.system_prompt  # triage-core
    assert "# Phase: validate the report" in spec.system_prompt


@pytest.mark.asyncio
async def test_package_notes_reach_their_phase():
    validation = {"verdict": "process", "reason": "merge", "short_title": "merge"}
    provider = FakeProvider([_session("submit_validation", validation)])
    await _pipeline(provider).triage(_payload(package="libvirt"))
    assert "AppArmor" in provider.specs[0].system_prompt


@pytest.mark.asyncio
async def test_update_without_escalation_is_one_session():
    review = {"action": "none", "reason": "a me-too comment"}
    provider = FakeProvider([_session("submit_update_review", review)])
    outcome = await _pipeline(provider).triage(_payload(status="Triaged"))
    assert len(provider.specs) == 1
    assert outcome.route == "update"
    assert outcome.result is not None
    assert outcome.result.status is TriageStatus.no_change
    assert "me-too" in outcome.result.analysis


@pytest.mark.asyncio
async def test_update_escalates_to_validate():
    review = {"action": "escalate", "reason": "new symptom"}
    validation = {
        "verdict": "incomplete",
        "reason": "no version",
        "short_title": "t",
        "comment": "Which version?",
    }
    provider = FakeProvider(
        [_session("submit_update_review", review), _session("submit_validation", validation)]
    )
    outcome = await _pipeline(provider).triage(_payload(status="Triaged"))
    assert len(provider.specs) == 2
    assert outcome.result is not None
    assert outcome.result.status is TriageStatus.incomplete
    assert [p for p, _ in _phases(outcome)] == ["precheck", "review-update", "validate-report"]


@pytest.mark.asyncio
async def test_bug_without_candidates_is_analysed_then_reported():
    provider = FakeProvider(
        [
            _session("submit_validation", BUG, input_tokens=1000, output_tokens=100),
            _session("submit_analysis", ANALYSIS, input_tokens=5000, output_tokens=500),
            _session("submit_report", REPORT, input_tokens=2000, output_tokens=200),
        ]
    )
    outcome = await _pipeline(provider).triage(_payload())
    assert _phases(outcome) == [
        ("precheck", "done"),
        ("validate-report", "done"),
        ("search", "skipped"),
        ("search-upstream", "skipped"),
        ("search-other-distros", "skipped"),
        ("assess-existing", "skipped"),
        ("check-regression", "skipped"),
        ("analyze-source", "done"),
        ("propose-fix", "skipped"),
        ("write-report", "done"),
    ]
    assert outcome.phases[-2].summary == "no fix known and no root cause found"
    assert outcome.result is not None
    assert outcome.result.status is TriageStatus.triaged
    assert outcome.result.short_title == "crash on start"
    assert '"package": "pkg"' in provider.specs[1].task
    report_task = provider.specs[2].task
    assert "Null pointer." in report_task
    assert provider.specs[2].tools[-1].name == "submit_report"
    assert len(provider.specs[2].tools) == 1  # write-report has no tools
    tokens = {p.phase: (p.input_tokens, p.output_tokens) for p in outcome.phases if p.sessions}
    assert tokens == {
        "validate-report": (1000, 100),
        "analyze-source": (5000, 500),
        "write-report": (2000, 200),
    }
    assert list(phase_totals([outcome])) == ["validate-report", "analyze-source", "write-report"]


@pytest.mark.asyncio
async def test_duplicate_skips_analysis_and_fix(monkeypatch):
    candidate = Candidate(
        source="launchpad",
        ref="42",
        url="https://bugs.launchpad.net/bugs/42",
        title="crash",
        status="Confirmed",
        package="pkg",
    )

    async def fake_search(lp, facts, own_bug, also):
        assert facts.search_terms == ["segfault on start"]
        return [candidate], 1

    monkeypatch.setattr(pipeline_module, "search_launchpad", fake_search)
    assessment = {"duplicate_of": "42", "summary": "Same crash as LP #42."}
    report = {"status": "Duplicate", "duplicate_of": "42", "analysis": "Dup."}
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_assessment", assessment),
            _session("submit_report", report),
        ]
    )
    outcome = await _pipeline(provider, lp=object()).triage(_payload())
    assert [p for p, s in _phases(outcome) if s == "done"] == [
        "precheck",
        "validate-report",
        "search",
        "assess-existing",
        "write-report",
    ]
    assert ("analyze-source", "skipped") in _phases(outcome)
    assert ("propose-fix", "skipped") in _phases(outcome)
    assert '"ref": "42"' in provider.specs[1].task
    assert outcome.result is not None
    assert outcome.result.duplicate_of == "42"


@pytest.mark.asyncio
async def test_report_check_rejects_unverifiable_tags_and_the_model_repairs():
    turn = FakeTurn(
        calls=[
            ("submit_report", {**REPORT, "tags": ["bitesize"]}),
            ("submit_report", REPORT),
        ]
    )
    provider = FakeProvider(
        [_session("submit_validation", BUG), _session("submit_analysis", ANALYSIS), [turn]]
    )
    outcome = await _pipeline(provider).triage(_payload())
    rejected = [out for name, _, out, ok in provider.calls if name == "submit_report" and not ok]
    assert len(rejected) == 1
    assert "bitesize" in rejected[0]
    assert outcome.result is not None
    assert outcome.result.tags == []


@pytest.mark.asyncio
async def test_failed_report_falls_back_to_a_host_result():
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_analysis", ANALYSIS),
            [FakeTurn(text="no"), FakeTurn(text="no")],
        ]
    )
    outcome = await _pipeline(provider).triage(_payload())
    assert ("write-report", "failed") in _phases(outcome)
    assert outcome.result is not None
    assert outcome.result.status is TriageStatus.no_change
    assert "server-triage-discuss" in outcome.result.tags
    assert "Null pointer." in outcome.result.analysis


@pytest.mark.asyncio
async def test_failed_validate_fails_the_bug():
    provider = FakeProvider([[FakeTurn(text="no"), FakeTurn(text="no")]])
    outcome = await _pipeline(provider).triage(_payload())
    assert outcome.result is None
    assert outcome.error == "validate-report failed: the agent did not call submit_validation"
    assert outcome.phases[-1].state is PhaseState.failed


@pytest.mark.asyncio
async def test_skill_feedback_is_logged(tmp_path):
    log = tmp_path / "feedback.jsonl"
    validation = {
        "verdict": "process",
        "reason": "merge",
        "short_title": "merge",
        "skill_feedback": "Say how to spot sync requests.",
    }
    provider = FakeProvider([_session("submit_validation", validation)])
    pipeline = _pipeline(provider, feedback=True, feedback_log=log, model="m1")
    outcomes = await triage_bugs(pipeline, [_payload()])
    assert outcomes[0].phases[-1].feedback == "Say how to spot sync requests."
    entry = json.loads(log.read_text().strip())
    assert entry["skill"] == "validate-report"
    assert entry["bug"] == "100"
    assert entry["model"] == "m1"
    assert entry["feedback"] == "Say how to spot sync requests."
    assert len(entry["skill_sha256"]) == 64


# --- search, upstream and regression phases --------------------------------


SOURCE_TOOLS = ["github_search", "bugzilla_search", "source_diff", "pkg_changelog"]
DIFF = "--- a/f.c\n+++ b/f.c\n@@\n-  free(p);\n+  free(p); free(p);\n"


def _networked(monkeypatch, *, hits=(), uploads=()):
    """A pipeline whose host searches are faked (no Launchpad, no network)."""

    async def no_upstream(http, package):
        return UpstreamInfo(homepage="https://example.org")

    async def no_debian(http, packages, terms):
        return []

    async def fake_hits(http, package, numbers, series):
        return list(hits)

    async def fake_uploads(lp, package, since):
        return [u.model_copy() for u in uploads]

    async def fake_previous(http, package, version):
        return "1.0-1"

    async def fake_diff(http, package, old, new):
        assert (old, new) == ("1.0-1", "1.0-1ubuntu0.1")
        return DIFF

    monkeypatch.setattr(archive_module, "read_upstream_info", no_upstream)
    monkeypatch.setattr(pipeline_module, "search_debian", no_debian)
    monkeypatch.setattr(pipeline_module, "changelog_hits", fake_hits)
    monkeypatch.setattr(archive_module, "previous_version", fake_previous)
    monkeypatch.setattr(checks_module, "read_diff", fake_diff)
    monkeypatch.setattr(archive_module, "uploads_since", fake_uploads)


def _with_source_tools(provider) -> Pipeline:
    pipeline = _pipeline(provider, http=object())
    tools = build_tools(ToolContext(), SOURCE_TOOLS)
    pipeline.tools |= {t.name: t for t in tools}
    return pipeline


@pytest.mark.asyncio
async def test_upstream_fix_skips_other_distros_and_is_assessed(monkeypatch):
    _networked(monkeypatch)
    finding = {
        "source": "upstream",
        "url": "https://github.com/o/pkg/issues/1",
        "title": "crash on start",
        "summary": "Same crash.",
        "claims_fix": True,
    }
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_upstream_search", {"findings": [finding], "summary": "Fixed upstream."}),
            _session("submit_assessment", {"summary": "Fixed upstream in 2.0."}),
            _session("submit_fix", {**FIX, "proposed_fix": {"kind": "reference", "value": finding["url"]}}),
            _session("submit_report", REPORT),
        ]
    )
    outcome = await _with_source_tools(provider).triage(_payload())
    states = dict(_phases(outcome))
    assert states["search-upstream"] == "done"
    assert states["search-other-distros"] == "skipped"
    assert states["analyze-source"] == "skipped"
    assert states["propose-fix"] == "done"
    skipped = next(p for p in outcome.phases if p.phase == "search-other-distros")
    assert skipped.summary == "upstream has a fix"
    assert '"claims_fix": true' in provider.specs[2].task
    assert '"claims_fix": true' in provider.specs[3].task
    assert '"homepage": "https://example.org"' in provider.specs[1].task
    assert outcome.result is not None
    assert outcome.result.proposed_fix.value == finding["url"]
    assert outcome.result.fix_applies == {}  # an issue is no patch to check


@pytest.mark.asyncio
async def test_bug_closed_in_a_changelog_skips_the_outside_search(monkeypatch):
    hit = ChangelogHit(bug="100", series="noble", version="1.0-2", entry="* fix (LP: #100)")
    _networked(monkeypatch, hits=[hit])
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_assessment", {"summary": "Fixed in 1.0-2."}),
            _session("submit_analysis", ANALYSIS),
            _session("submit_report", REPORT),
        ]
    )
    outcome = await _with_source_tools(provider).triage(_payload())
    upstream = next(p for p in outcome.phases if p.phase == "search-upstream")
    assert upstream.state is PhaseState.skipped
    assert upstream.summary == "already closed by 1.0-2 (noble)"
    assert '"version": "1.0-2"' in provider.specs[1].task


@pytest.mark.asyncio
async def test_confirmed_regression_checks_the_hunk_and_allows_the_tag(monkeypatch):
    upload = Upload(series="noble", version="1.0-1ubuntu0.1", pockets=["Updates"], published="2026-10-01")
    _networked(monkeypatch, uploads=[upload])
    regression = {
        "confirmed": True,
        "suspect_version": "1.0-1ubuntu0.1",
        "previous_version": "1.0-1",
        "summary": "The update frees p twice.",
    }
    nothing = {"summary": "Nothing found."}
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_upstream_search", nothing),
            _session("submit_distro_search", nothing),
            [
                FakeTurn(
                    calls=[
                        ("submit_regression", {**regression, "hunk": "+  free(q);"}),
                        ("submit_regression", {**regression, "hunk": "+  free(p); free(p);"}),
                    ]
                )
            ],
            _session("submit_fix", FIX),
            _session("submit_report", {**REPORT, "tags": ["regression-update"]}),
        ]
    )
    outcome = await _with_source_tools(provider).triage(_payload())
    rejected = [out for name, _, out, ok in provider.calls if name == "submit_regression" and not ok]
    assert len(rejected) == 1
    assert "is not in the diff" in rejected[0]
    states = dict(_phases(outcome))
    assert states["assess-existing"] == "skipped"
    assert states["check-regression"] == "done"
    assert states["analyze-source"] == "skipped"
    assert '"previous": "1.0-1"' in provider.specs[3].task
    assert '"hunk": "+  free(p); free(p);"' in provider.specs[4].task  # propose-fix
    assert outcome.result is not None
    assert outcome.result.tags == ["regression-update"]


@pytest.mark.asyncio
async def test_phase_needing_a_disabled_tool_is_skipped(monkeypatch):
    _networked(monkeypatch)
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_analysis", ANALYSIS),
            _session("submit_report", REPORT),
        ]
    )
    outcome = await _pipeline(provider, http=object()).triage(_payload())
    upstream = next(p for p in outcome.phases if p.phase == "search-upstream")
    assert upstream.state is PhaseState.skipped
    assert upstream.summary.startswith("needs github_search or gitlab_search or bugzilla_search")


# --- analyze-source and propose-fix -------------------------------------------

TREE = {
    "src/a.c": "".join(f"line {n}\n" for n in range(1, 11)),
    "debian/changelog": "pkg (1.0-2) devel; urgency=medium\n",
}
GOOD_DIFF = "--- a/src/a.c\n+++ b/src/a.c\n@@ -4,3 +4,3 @@\n line 4\n-line 5\n+line five\n line 6\n"
BAD_DIFF = GOOD_DIFF.replace("-line 5", "-line 55")


def _fake_tree(monkeypatch, stale: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """Serve TREE to the host checks, at any ref; returns the (path, ref) reads.

    ``stale`` refs answer an old debian/changelog, like a git-ubuntu applied/
    branch that stopped being updated.
    """
    reads: list[tuple[str, str]] = []

    async def fake_read(http, package, path, ref):
        reads.append((path, ref))
        if path == "debian/changelog" and ref in stale:
            return "pkg (0.1-1) precise; urgency=low\n"
        if path not in TREE:
            raise ToolError(f"{path} at {ref} not found in git-ubuntu {package}")
        return TREE[path]

    monkeypatch.setattr(checks_module, "read_file", fake_read)
    monkeypatch.setattr(patch_module, "read_file", fake_read)
    monkeypatch.setattr(source_module, "read_file", fake_read)
    return reads


@pytest.mark.asyncio
async def test_analysis_locations_and_the_fix_are_checked_and_allow_bitesize(monkeypatch):
    _networked(monkeypatch)
    reads = _fake_tree(monkeypatch)
    high = {**ANALYSIS, "confidence": "high"}
    bad = [
        {"path": "src/a.c", "line": 50, "ref": "noble", "note": "x"},
        {"path": "src/b.c", "line": 1, "note": "y"},
    ]
    good = [{"path": "src/a.c", "line": 5, "ref": "noble", "note": "the bad line"}]
    diff = {"kind": "diff", "value": GOOD_DIFF}
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            [
                FakeTurn(
                    calls=[
                        ("submit_analysis", {**high, "locations": bad}),
                        ("submit_analysis", {**high, "locations": good}),
                    ]
                )
            ],
            [
                FakeTurn(
                    calls=[
                        ("submit_fix", {**FIX, "proposed_fix": {"kind": "diff", "value": BAD_DIFF}}),
                        ("submit_fix", {**FIX, "proposed_fix": diff}),
                    ]
                )
            ],
            _session("submit_report", {**REPORT, "tags": ["bitesize"]}),
        ]
    )
    outcome = await _pipeline(provider, http=object()).triage(_payload())
    rejected = [(name, out) for name, _, out, ok in provider.calls if not ok]
    assert [name for name, _ in rejected] == ["submit_analysis", "submit_fix"]
    assert "src/a.c:50: the file has only 10 lines at applied/ubuntu/noble-devel" in rejected[0][1]
    assert "location src/b.c: src/b.c at applied/ubuntu/devel not found" in rejected[0][1]
    assert "applies to none of the checked series" in rejected[1][1]
    assert ("src/a.c", "applied/ubuntu/devel") in reads
    states = dict(_phases(outcome))
    assert states["analyze-source"] == "done"
    assert states["propose-fix"] == "done"
    assert '"confidence": "high"' in provider.specs[2].task  # propose-fix sees the analysis
    assert '"devel": "clean"' in provider.specs[3].task  # write-report sees the host check
    assert '"bitesize_verified": true' in provider.specs[3].task
    assert [s.max_calls for s in provider.specs[1:3]] == [20, 12]  # skill budgets
    assert outcome.result is not None
    assert outcome.result.proposed_fix.value == GOOD_DIFF
    assert outcome.result.fix_applies == {"devel": "clean"}
    assert outcome.result.tags == ["bitesize"]


@pytest.mark.asyncio
async def test_a_fix_that_is_already_applied_does_not_allow_bitesize(monkeypatch):
    _networked(monkeypatch)
    _fake_tree(monkeypatch)
    applied = GOOD_DIFF.replace("-line 5\n+line five", "-line zero\n+line 5")
    turn = FakeTurn(calls=[("submit_report", {**REPORT, "tags": ["bitesize"]}), ("submit_report", REPORT)])
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_analysis", {**ANALYSIS, "confidence": "medium"}),
            _session("submit_fix", {**FIX, "proposed_fix": {"kind": "diff", "value": applied}}),
            [turn],
        ]
    )
    outcome = await _pipeline(provider, http=object()).triage(_payload())
    assert outcome.result is not None
    assert outcome.result.fix_applies == {"devel": "already applied"}
    assert outcome.result.tags == []


@pytest.mark.asyncio
async def test_a_stale_patches_applied_tree_is_replaced_and_does_not_allow_bitesize(monkeypatch):
    _networked(monkeypatch)
    reads = _fake_tree(monkeypatch, stale=frozenset({"applied/ubuntu/devel"}))
    turn = FakeTurn(calls=[("submit_report", {**REPORT, "tags": ["bitesize"]}), ("submit_report", REPORT)])
    provider = FakeProvider(
        [
            _session("submit_validation", BUG),
            _session("submit_analysis", {**ANALYSIS, "confidence": "medium"}),
            _session("submit_fix", {**FIX, "proposed_fix": {"kind": "diff", "value": GOOD_DIFF}}),
            [turn],
        ]
    )
    outcome = await _pipeline(provider, http=object()).triage(_payload())
    assert ("src/a.c", "ubuntu/devel") in reads
    assert ("src/a.c", "applied/ubuntu/devel") not in reads
    assert outcome.result is not None
    assert outcome.result.fix_applies == {"devel": "clean [without the package's patches applied]"}
    assert outcome.result.tags == []
    rejected = [out for name, _, out, ok in provider.calls if name == "submit_report" and not ok]
    assert "bitesize" in rejected[0]


@pytest.mark.asyncio
async def test_offline_end_to_end_analysis_and_fix_against_recorded_git_ubuntu(monkeypatch):
    """The nginx regex fix: the model reads the code, then checks the upstream commit."""
    _networked(monkeypatch)

    async def public(url: str) -> None:
        return None

    async def series(lp, package):
        return {"noble": {"published": {"Release": "1.24.0-2ubuntu7"}}}

    monkeypatch.setattr(tools_module.http, "check_public_url", public)
    monkeypatch.setattr(archive_module, "series", series)
    session = ReplaySession("source")
    http = ReadOnlyHTTP(session)  # ty: ignore[invalid-argument-type]
    names = ["source_symbols", "source_file", "patch_applies", "fetch_patch"]
    tools = {t.name: t for t in build_tools(ToolContext(http=http), names)}
    symbols, read = TOOL_CALLS["source"][2], TOOL_CALLS["source"][5]
    location = {"path": "src/http/ngx_http_script.c", "line": 1217, "ref": "noble", "note": "is_args"}
    analysis = {
        "root_cause": "ngx_http_script_regex_end_code does not reset e->is_args.",
        "confidence": "high",
        "locations": [location],
        "summary": "Found the regex end code.",
    }
    fix = {"proposed_fix": {"kind": "reference", "value": NGINX_FIX}, "summary": "Upstream fixed it."}
    check = {"package": "nginx", "url": NGINX_FIX, "ref": "noble,devel"}
    provider = FakeProvider(
        [
            _session(
                "submit_validation",
                {**BUG, "facts": {**FACTS, "package": "nginx", "affected_series": ["noble"]}},
            ),
            [FakeTurn(calls=[symbols, read]), FakeTurn(calls=[("submit_analysis", analysis)])],
            [FakeTurn(calls=[("patch_applies", check)]), FakeTurn(calls=[("submit_fix", fix)])],
            _session("submit_report", REPORT),
        ]
    )
    pipeline = Pipeline(provider=provider, tools=tools, http=http, team=TEAM, window=WINDOW)
    outcome = await pipeline.triage(_payload(package="nginx"))
    assert [(name, ok) for name, _, _, ok in provider.calls] == [
        ("submit_validation", True),
        ("source_symbols", True),
        ("source_file", True),
        ("submit_analysis", True),
        ("patch_applies", True),
        ("submit_fix", True),
        ("submit_report", True),
    ]
    applies = next(out for name, _, out, _ in provider.calls if name == "patch_applies")
    assert "applied/ubuntu/noble-devel: already applied" in applies
    assert '"ref": "noble"' in provider.specs[2].task
    assert outcome.result is not None
    assert outcome.result.fix_applies == {"noble": "already applied", "devel": "already applied"}
    assert outcome.result.proposed_fix.value == NGINX_FIX
    assert '"bitesize_verified": false' in provider.specs[3].task


# --- triage_bugs: logs and progress -----------------------------------------


@pytest.mark.asyncio
async def test_triage_bugs_logs_routes_verdicts_and_totals(caplog):
    validation = {"verdict": "process", "reason": "merge", "short_title": "merge"}
    provider = FakeProvider([_session("submit_validation", validation, input_tokens=10, output_tokens=2)])
    payloads = [_payload(title="[MIR] pkg"), _payload(number="200", private=True)]
    with caplog.at_level(logging.INFO, logger="startriage.ai"):
        outcomes = await triage_bugs(_pipeline(provider), payloads)
    messages = [r.getMessage() for r in caplog.records]
    assert any("Triaging bug 100 (1/2)" in m for m in messages)
    assert any("Bug 100 verdict=process, process hints: MIR" in m for m in messages)
    assert any("Bug 100 → status=no-change" in m for m in messages)
    assert any("Bug 200 route=denied" in m for m in messages)
    assert any("1 succeeded, 0 failed, 1 not sent" in m for m in messages)
    assert any("Phase validate-report: 1 sessions, 10 input / 2 output tokens" in m for m in messages)
    assert [o.ok for o in outcomes] == [True, False]


@pytest.mark.asyncio
async def test_triage_bugs_logs_failure_as_warning(caplog):
    provider = FakeProvider([[FakeTurn(text="no result")]])
    with caplog.at_level(logging.INFO, logger="startriage.ai"):
        await triage_bugs(_pipeline(provider), [_payload(number="999")])
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Bug 999 failed" in m for m in warnings)


@pytest.mark.asyncio
async def test_triage_bugs_reports_progress_and_phases():
    validation = {"verdict": "process", "reason": "merge", "short_title": "merge"}
    provider = FakeProvider([_session("submit_validation", validation)] * 2)
    seen: list = []
    pipeline = _pipeline(provider, on_phase=seen.append)
    await triage_bugs(
        pipeline,
        [_payload(number="100"), _payload(number="200")],
        on_progress=lambda index, total, bug: seen.append((index, total, bug)),
    )
    assert seen == [(1, 2, "100"), "validate-report", (2, 2, "200"), "validate-report"]
