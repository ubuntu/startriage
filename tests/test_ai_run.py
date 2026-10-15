"""Tests for AI orchestration (run.py) and CLI wiring — all offline."""

from __future__ import annotations

import pytest

from startriage.ai import (
    FakeProvider,
    FakeTurn,
    parse_bug_number,
    payloads_from_tasks,
    run_agent_on_payloads,
)
from startriage.cli import _build_parser
from startriage.config import AIConfig, StarTriageConfig

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
    payloads = payloads_from_tasks(tasks)  # ty: ignore[invalid-argument-type]
    assert [p["number"] for p in payloads] == ["1", "2"]


def test_payloads_from_tasks_skips_failures():
    tasks = [_FakeTask("1"), _FakeTask("2", raises=True), _FakeTask("3")]
    payloads = payloads_from_tasks(tasks)  # ty: ignore[invalid-argument-type]
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
