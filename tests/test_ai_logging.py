"""Tests for AI step logging (observability under -v / -vv)."""

from __future__ import annotations

import logging

import pytest

from startriage.ai import FakeProvider, triage_bugs

_OK = """```json
{
  "bug": "123",
  "package": "pkg",
  "short_title": "boom",
  "status": "Triaged",
  "tags": ["server-todo"],
  "analysis": "It broke.",
  "thought_process": "Checked the logs.",
  "proposed_fix": {"kind": "none", "value": ""},
  "references": [],
  "suggested_improvements": ""
}
```"""


@pytest.mark.asyncio
async def test_triage_bugs_logs_progress_and_decision(caplog):
    provider = FakeProvider([_OK])
    with caplog.at_level(logging.INFO, logger="startriage.ai.agent"):
        await triage_bugs(provider, [{"number": "123"}], system_prompt="sys")

    messages = [r.getMessage() for r in caplog.records]
    assert any("Triaging bug 123 (1/1)" in m for m in messages)
    assert any("Bug 123 → status=Triaged" in m for m in messages)
    assert any("1 succeeded, 0 failed" in m for m in messages)


@pytest.mark.asyncio
async def test_triage_bugs_logs_failure_as_warning(caplog):
    provider = FakeProvider(["no json here"])
    with caplog.at_level(logging.INFO, logger="startriage.ai.agent"):
        await triage_bugs(provider, [{"number": "999"}], system_prompt="sys")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Bug 999 failed" in m for m in warnings)
    summaries = [r.getMessage() for r in caplog.records if "complete" in r.getMessage()]
    assert any("0 succeeded, 1 failed" in m for m in summaries)


@pytest.mark.asyncio
async def test_triage_bugs_debug_logs_thought_process(caplog):
    provider = FakeProvider([_OK])
    with caplog.at_level(logging.DEBUG, logger="startriage.ai.agent"):
        await triage_bugs(provider, [{"number": "123"}], system_prompt="sys")

    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("thought process: Checked the logs." in m for m in debug)
