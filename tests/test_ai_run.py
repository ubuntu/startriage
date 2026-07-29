"""Tests for AI orchestration (run.py) and CLI wiring — all offline."""

from __future__ import annotations

import pytest

from startriage.ai import (
    FakeProvider,
    parse_bug_number,
    payloads_from_tasks,
    run_agent_on_payloads,
)
from startriage.cli import _build_parser
from startriage.config import StarTriageConfig
from startriage.enums import AIPermission

_CANNED = """Here is my analysis.

```json
{
  "bug": "123",
  "package": "pkg",
  "short_title": "boom on start",
  "status": "Triaged",
  "tags": ["server-todo"],
  "analysis": "It broke.",
  "thought_process": "Looked at logs.",
  "proposed_fix": {"kind": "none", "value": ""},
  "references": [],
  "suggested_improvements": ""
}
```
"""


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
    payloads = [{"number": "123", "title": "boom"}]

    report = await run_agent_on_payloads(config, payloads, provider=provider)

    assert report is not None
    assert "## LP #123 — pkg — boom on start" in report
    assert "**Suggested status:** Triaged" in report
    # The agent was asked exactly once, with the payload as the user message.
    assert len(provider.calls) == 1
    assert '"number": "123"' in provider.calls[0][1]


@pytest.mark.asyncio
async def test_run_agent_on_payloads_empty_returns_none():
    provider = FakeProvider([])
    report = await run_agent_on_payloads(StarTriageConfig(), [], provider=provider)
    assert report is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_run_agent_on_payloads_records_bad_agent_output():
    provider = FakeProvider(["no json here"])
    report = await run_agent_on_payloads(
        StarTriageConfig(),
        [{"number": "999"}],
        provider=provider,
    )
    assert report is not None
    assert "## LP #999 — triage failed" in report


# --- CLI parser wiring -----------------------------------------------------


def test_parser_analyze_accepts_multiple_bugs():
    args = _build_parser().parse_args(["analyze", "123", "#456", "https://x/+bug/789"])
    assert args.bug == ["123", "#456", "https://x/+bug/789"]
    assert args.func.__name__ == "_run_analyze"
    assert args.ai is None


def test_parser_analyze_ai_flag():
    # No --ai -> off; --ai requires an explicit level; a bug number is not a level.
    assert _build_parser().parse_args(["analyze", "123"]).ai is None
    assert _build_parser().parse_args(["analyze", "--ai", "full", "123"]).ai is AIPermission.full
    assert _build_parser().parse_args(["analyze", "--ai", "ask", "123"]).ai is AIPermission.ask
    args = _build_parser().parse_args(["analyze", "--ai", "restricted", "123"])
    assert args.ai is AIPermission.restricted
    assert args.bug == ["123"]


def test_parser_triage_ai_flag_defaults_none():
    assert _build_parser().parse_args(["triage"]).ai is None
    assert _build_parser().parse_args(["triage", "--ai", "restricted"]).ai is AIPermission.restricted
    assert _build_parser().parse_args(["triage", "--ai", "full"]).ai is AIPermission.full


def test_parser_ai_flag_requires_a_level():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["triage", "--ai"])


def test_parser_ai_flag_rejects_unknown_level():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["triage", "--ai", "bogus"])
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["analyze", "--ai", "123"])
