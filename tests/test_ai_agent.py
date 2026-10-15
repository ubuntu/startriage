"""Tests for submit_<phase> tools and the phase runner."""

from __future__ import annotations

import pytest

from startriage.ai import FakeProvider, FakeTurn, run_phase
from startriage.ai.phases import (
    Assessment,
    FixProposal,
    PhaseState,
    Report,
    UpdateReview,
    ValidateResult,
)
from startriage.ai.tools import Submission, ToolError
from startriage.ai.tools.submit import submit_schema

_REPORT = {"status": "Triaged", "analysis": "It crashes.", "tags": ["server-todo"]}


def _submission(model=Report, **kwargs) -> Submission:
    return Submission("submit_report", model, description="Submit.", **kwargs)


# --- Submission -------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_decodes_objects_sent_as_json_strings():
    submission = Submission("submit_fix", FixProposal, description="Submit.")
    fix = '{"kind": "diff", "value": "--- a/f\\n+++ b/f\\n"}'
    await submission.tool().handler({"proposed_fix": fix, "summary": "[x] a list-like string"})
    assert submission.result is not None
    assert submission.result.proposed_fix.kind.value == "diff"
    assert submission.result.summary == "[x] a list-like string"
    assert submission.rejections == 0


@pytest.mark.parametrize(
    "bad",
    [
        {**_REPORT, "status": "Bogus"},
        {"analysis": "no status"},
        # Incomplete / Invalid need a comment; Duplicate needs duplicate_of.
        {"status": "Incomplete", "analysis": "x"},
        {"status": "Duplicate", "analysis": "x"},
    ],
)
@pytest.mark.asyncio
async def test_submit_invalid_is_returned_for_repair(bad):
    submission = _submission()
    with pytest.raises(ToolError, match="call submit_report again"):
        await submission.tool().handler(bad)
    assert submission.result is None
    assert submission.rejections == 1


@pytest.mark.asyncio
async def test_skill_feedback_is_popped_and_kept_only_when_enabled():
    on = _submission(feedback=True)
    await on.tool().handler({**_REPORT, "skill_feedback": " say which series to use "})
    assert on.feedback == "say which series to use"
    assert "skill_feedback" in on.tool().parameters["properties"]

    off = _submission()
    await off.tool().handler({**_REPORT, "skill_feedback": "ignored"})
    assert off.feedback == ""
    assert "skill_feedback" not in off.tool().parameters["properties"]


def test_submit_schema_is_inlined():
    schema = submit_schema(Assessment)
    assert "$defs" not in schema
    assert "$ref" not in str(schema)
    assert "title" not in schema


@pytest.mark.parametrize(
    ("model", "args", "error"),
    [
        (UpdateReview, {"action": "comment", "reason": "r"}, "comment is required"),
        (UpdateReview, {"action": "change", "reason": "r"}, "status or tags"),
        (ValidateResult, {"verdict": "bug", "reason": "r", "short_title": "t"}, "facts is required"),
        (ValidateResult, {"verdict": "support", "reason": "r", "short_title": "t"}, "comment is required"),
    ],
)
def test_handoff_rules(model, args, error):
    with pytest.raises(ValueError, match=error):
        model.model_validate(args)


# --- run_phase --------------------------------------------------------------


async def _run(provider, model=Report):
    return await run_phase(
        provider,
        phase="write-report",
        bug="1",
        system_prompt="SYS",
        task="TASK",
        tools=[],
        submission=_submission(model),
    )


@pytest.mark.asyncio
async def test_run_phase_nudges_once():
    provider = FakeProvider([[FakeTurn(text="Triaged."), FakeTurn(calls=[("submit_report", _REPORT)])]])
    outcome = await _run(provider)
    assert outcome.state is PhaseState.done
    assert [call[0] for call in provider.calls] == ["submit_report"]


@pytest.mark.asyncio
async def test_run_phase_aborts_a_looping_agent():
    bad = {**_REPORT, "status": "Bogus"}
    provider = FakeProvider([[FakeTurn(calls=[("submit_report", bad)] * 10)]])
    outcome = await _run(provider)
    assert outcome.state is PhaseState.failed
    assert "aborted" in outcome.summary


@pytest.mark.asyncio
async def test_run_phase_records_provider_exception():
    class _BoomProvider(FakeProvider):
        async def run(self, spec):
            raise RuntimeError("network down")

    outcome = await _run(_BoomProvider())
    assert outcome.state is PhaseState.failed
    assert "provider error: network down" in outcome.summary
