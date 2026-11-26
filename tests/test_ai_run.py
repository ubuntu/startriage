"""Tests for AI orchestration (run.py) and CLI wiring — all offline."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import cast

import pytest

from startriage.ai import (
    AgentResult,
    FakeProvider,
    FakeTurn,
    ProposedFix,
    RunOptions,
    analyze_work_items,
    parse_bug_number,
    payloads_from_tasks,
    run_agent_on_payloads,
    work_item_from_payload,
)
from startriage.ai.contract import Importance, ProposedFixKind, SRUCandidate, TriageStatus
from startriage.ai.results import derive_actions
from startriage.analysis import ActionKind, ActionReadiness, CodeLocationInfo, FixCheckInfo
from startriage.cli import _build_parser
from startriage.config import AIConfig, StarTriageConfig, load_config
from startriage.output import OutputConfig, OutputFormat
from startriage.report import emit_triage_outputs
from startriage.sources.github.models import Issue, RepoResult
from startriage.sources.github.triage import GithubTriage
from startriage.sources.launchpad.models import Task
from startriage.triage import build_filter

_VALIDATION = {
    "verdict": "support",
    "reason": "a configuration question",
    "short_title": "boom on start",
    "comment": "Please ask on Discourse.",
}
_CANNED = [FakeTurn(calls=[("submit_validation", _VALIDATION)])]


@pytest.fixture(autouse=True)
def _cache_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


# --- parse_bug_number ------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("123456", "123456"),
        ("#123456", "123456"),
        ("https://bugs.launchpad.net/ubuntu/+bug/123456", "123456"),
        ("https://bugs.launchpad.net/ubuntu/+source/python3.12/+bug/987", "987"),
        ("  #42  ", "42"),
        ("https://launchpad.net/bugs/555", "555"),
    ],
)
def test_parse_bug_number(spec, expected):
    assert parse_bug_number(spec) == expected


@pytest.mark.parametrize(
    "spec",
    [
        "not-a-bug",
        "",
        "12ab",
        # Arbitrary URLs that merely contain digits must NOT resolve to a bug.
        "https://myponyadventure.lol/pages/3133742/cute.png",
        "https://example.com/issues/42",
    ],
)
def test_parse_bug_number_invalid(spec):
    with pytest.raises(ValueError):
        parse_bug_number(spec)


# --- payloads_from_tasks ---------------------------------------------------


class _FakeTask:
    def __init__(self, number: str, payload=None, *, raises: bool = False):
        self.number = number
        self._payload = payload if payload is not None else {"number": number}
        self._raises = raises

    def to_agent_payload(self):
        if self._raises:
            raise RuntimeError("boom")
        return self._payload


def test_payloads_from_tasks_dedupes_by_number():
    tasks = [_FakeTask("1"), _FakeTask("1"), _FakeTask("2")]
    payloads = payloads_from_tasks(cast(list[Task], tasks))
    assert [p["number"] for p in payloads] == ["1", "2"]


def test_payloads_from_tasks_skips_failures():
    tasks = [_FakeTask("1"), _FakeTask("2", raises=True), _FakeTask("3")]
    payloads = payloads_from_tasks(cast(list[Task], tasks))
    assert [p["number"] for p in payloads] == ["1", "3"]


# --- run_agent_on_payloads -------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_on_payloads_returns_markdown():
    provider = FakeProvider([_CANNED])
    config = StarTriageConfig()
    payloads = [{"number": "123", "package": "pkg", "title": "boom"}]

    report = await run_agent_on_payloads(config, payloads, provider=provider)

    assert report is not None
    assert "## LP #123 — pkg — boom on start" in report
    assert "**Route:** new" in report
    assert "**Suggested status:** Invalid" in report
    assert "> Please ask on Discourse." in report
    # One session: validate-report, with the payload in the task and its own tools.
    assert len(provider.specs) == 1
    spec = provider.specs[0]
    assert '"number": "123"' in spec.task
    assert [t.name for t in spec.tools] == ["lp_get_bug", "lp_get_attachment", "submit_validation"]
    assert (
        "_Agent tools: lp_get_bug, lp_get_attachment, lp_search_bugs, pkg_versions, pkg_changelog," in report
    )
    assert "## Cost" in report
    # The per-run scratch dir is removed afterwards.
    assert spec.working_directory is not None
    assert not spec.working_directory.exists()


@pytest.mark.asyncio
async def test_run_agent_on_payloads_uses_chosen_tools():
    provider = FakeProvider([_CANNED])
    config = StarTriageConfig(ai=AIConfig(tools={"lp_get_attachment": False}))
    report = await run_agent_on_payloads(config, [{"number": "1"}], provider=provider)
    names = [t.name for t in provider.specs[0].tools]
    assert names == ["lp_get_bug", "submit_validation"]
    assert report is not None
    assert "lp_get_attachment" not in report


# --- CLI parser wiring -----------------------------------------------------


def test_parser_analyze_accepts_multiple_bugs():
    args = _build_parser().parse_args(["analyze", "123", "#456", "https://x/+bug/789"])
    assert args.bug == ["123", "#456", "https://x/+bug/789"]
    assert args.func.__name__ == "_run_analyze"
    assert args.ai is False


def test_parser_ai_is_a_plain_flag():
    assert _build_parser().parse_args(["analyze", "123"]).ai is False
    args = _build_parser().parse_args(["analyze", "--ai", "123"])
    assert args.ai is True
    assert args.bug == ["123"]
    assert _build_parser().parse_args(["triage"]).ai is False
    assert _build_parser().parse_args(["triage", "--ai"]).ai is True
    assert _build_parser().parse_args(["triage"]).ai_keep_workdir is False
    assert _build_parser().parse_args(["analyze", "--ai", "--ai-keep-workdir", "1"]).ai_keep_workdir is True
    assert _build_parser().parse_args(["triage"]).ai_show_thinking is False
    assert _build_parser().parse_args(["triage", "--ai", "--ai-show-thinking"]).ai_show_thinking is True
    assert _build_parser().parse_args(["analyze", "--ai", "--ai-show-thinking", "1"]).ai_show_thinking is True
    assert _build_parser().parse_args(["triage"]).ai_json is None
    assert _build_parser().parse_args(["triage", "--ai", "--ai-json", "out.json"]).ai_json == "out.json"
    assert _build_parser().parse_args(["analyze", "--ai", "--ai-json", "a.json", "1"]).ai_json == "a.json"


# --- analyze_work_items, live progress & thinking --------------------------


@pytest.mark.asyncio
async def test_analyze_work_items_streams_progress_and_thinking():
    turn = FakeTurn(
        reasoning="Checking whether this is a config question.\nYes, ask on Discourse.",
        calls=[("submit_validation", _VALIDATION)],
        input_tokens=400,
        output_tokens=50,
    )
    provider = FakeProvider([[turn], []])
    config = StarTriageConfig()
    items = [
        work_item_from_payload({"number": "123", "package": "pkg", "title": "boom", "status": "New"}),
        work_item_from_payload(
            {"number": "456", "package": "pkg", "title": "secret", "private": True, "expiring": True}
        ),
    ]
    progress_buf = io.StringIO()

    batch = await analyze_work_items(
        config,
        items,
        provider=provider,
        options=RunOptions(
            verbose=True,
            show_thinking=True,
            progress_stream=progress_buf,
            progress_tty=False,
        ),
    )

    assert batch is not None
    assert provider.specs[0].streaming is True
    assert len(batch.run.items) == 2
    assert batch.run.items[0].snapshot.status == "New"
    assert batch.run.items[1].snapshot.expiring is True
    assert [a.kind for a in batch.run.items[0].actions] == [ActionKind.tracker_update]

    out = progress_buf.getvalue()
    assert "Checking whether this is a config question." in out
    assert "[1/2] LP #123 (pkg): Invalid" in out
    assert "[2/2] LP #456 (pkg): private (not sent)" in out


def test_derive_actions_produces_multi_action_list_with_readiness():
    result = AgentResult(
        bug="200",
        package="nginx",
        status=TriageStatus.triaged,
        importance=Importance.high,
        series={"noble": TriageStatus.triaged, "questing": TriageStatus.fix_released},
        tags=["server-todo", "bitesize"],
        proposed_fix=ProposedFix(kind=ProposedFixKind.reference, value="https://example.test/commit/1"),
        fix_checks={
            "noble": FixCheckInfo(ref="noble", state="clean", patches_applied=True),
            "devel": FixCheckInfo(ref="devel", state="already applied", patches_applied=True),
        },
        bitesize_verified=True,
        locations=[CodeLocationInfo(path="src/ngx.c", line=42, ref="devel", note="null deref")],
        sru_candidates=[SRUCandidate(series="noble", reason="clean backport")],
        upstream_suggestion="Mention Ubuntu backport.",
        upstream_url="https://github.com/nginx/nginx",
    )

    actions = derive_actions(result)

    assert [a.kind for a in actions] == [
        ActionKind.tracker_update,
        ActionKind.prepare_fix,
        ActionKind.upstream_report,
    ]
    fix_action = actions[1]
    assert fix_action.readiness is ActionReadiness.ready
    assert fix_action.params["package"] == "nginx"
    assert fix_action.params["strategy"] == "upstream_reference"
    assert fix_action.params["target_series"] == ["noble"]
    assert fix_action.params["bitesize_verified"] is True

    # Unmerged upstream fix drops readiness to needs_review.
    unmerged = result.model_copy(update={"fix_unmerged": True})
    assert derive_actions(unmerged)[1].readiness is ActionReadiness.needs_review

    # Review-update "no action" yields a single none action.
    noop = AgentResult(bug="201", package="nginx", status=TriageStatus.no_change)
    assert [a.kind for a in derive_actions(noop)] == [ActionKind.none]


# --- Stage 3 emit_triage_outputs -------------------------------------------


@pytest.mark.asyncio
async def test_emit_triage_outputs_suppresses_stdout_with_html_and_includes_ai_in_json(tmp_path):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    issue = Issue(1, "boom", "https://github.com/o/r/issues/1", "https://github.com/o/r", now, now, "open")
    gh = GithubTriage(start=now.date(), end=now.date(), results=[RepoResult("o/r", issues=[issue])])
    config = load_config(tmp_path / "none.toml")
    opts = build_filter(config, interval="2026-10-01", sources="github")

    provider = FakeProvider([_CANNED])
    item = work_item_from_payload({"number": "123", "package": "pkg", "title": "boom"})
    batch = await analyze_work_items(
        config,
        [item],
        provider=provider,
        options=RunOptions(progress_stream=io.StringIO(), progress_tty=False),
    )
    assert batch is not None
    ai_md = batch.to_markdown()

    # With --html/--markdown/--ai-json, stdout receives no AI dump and each file gets the AI data.
    html_path = tmp_path / "triage.html"
    md_path = tmp_path / "triage.md"
    ai_json_path = tmp_path / "ai.json"
    stdout = io.StringIO()
    await emit_triage_outputs(
        {"github": gh},
        opts,
        OutputConfig(
            fmt=OutputFormat.TERMINAL,
            out=stdout,
            html_path=html_path,
            markdown_path=md_path,
            ai_json_path=ai_json_path,
        ),
        ai_run=batch.run,
        ai_markdown=ai_md,
    )
    assert stdout.getvalue() == ""
    assert "Automated triage" in md_path.read_text()
    assert '<script type="application/json" id="ai-data">' in html_path.read_text()
    saved_ai = json.loads(ai_json_path.read_text())
    assert saved_ai["schema_version"] == 1
    assert saved_ai["items"][0]["key"] == "123"
    assert [a["kind"] for a in saved_ai["items"][0]["actions"]] == ["tracker_update"]

    # Without --html/--markdown, AI markdown goes to stdout.
    stdout_only = io.StringIO()
    await emit_triage_outputs(
        {"github": gh},
        opts,
        OutputConfig(fmt=OutputFormat.TERMINAL, out=stdout_only),
        ai_run=batch.run,
        ai_markdown=ai_md,
    )
    assert "## LP #123 — pkg — boom on start" in stdout_only.getvalue()

    # With --format json, AI run is attached under "ai".
    json_out = io.StringIO()
    await emit_triage_outputs(
        {"github": gh},
        opts,
        OutputConfig(fmt=OutputFormat.JSON, out=json_out),
        ai_run=batch.run,
        ai_markdown=ai_md,
    )
    parsed = json.loads(json_out.getvalue())
    assert parsed["ai"]["schema_version"] == 1
    assert parsed["ai"]["items"][0]["key"] == "123"
