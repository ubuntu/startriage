"""Tests for the agent result contract and the sequential triage loop."""

from __future__ import annotations

import json

import pytest

from startriage.ai import (
    AgentResult,
    AgentResultError,
    FakeProvider,
    extract_json_block,
    load_system_prompt,
    parse_agent_result,
    triage_bugs,
)
from startriage.enums import ProposedFixKind, TriageStatus

_VALID_RESULT = {
    "bug": "123",
    "package": "pkg",
    "short_title": "boom on start",
    "status": "Triaged",
    "tags": ["server-todo"],
    "analysis": "It crashes.",
    "thought_process": "Looked at logs.",
    "proposed_fix": {"kind": "reference", "value": "https://example.test/commit"},
    "references": ["https://bugs.launchpad.net/ubuntu/+bug/123"],
    "suggested_improvements": "Add a cache.",
}


def _fenced(payload: dict, lang: str = "json") -> str:
    return f"Here is the result:\n\n```{lang}\n{json.dumps(payload)}\n```\n"


# --- extract_json_block ----------------------------------------------------


def test_extract_json_block_basic():
    text = 'preamble\n```json\n{"a": 1}\n```\ntrailer'
    assert extract_json_block(text) == '{"a": 1}'


def test_extract_json_block_prefers_last_json_block():
    text = '```json\n{"first": true}\n```\n```json\n{"second": true}\n```'
    assert extract_json_block(text) == '{"second": true}'


def test_extract_json_block_falls_back_to_untagged_block():
    text = 'no json tag here\n```\n{"untagged": 1}\n```'
    assert extract_json_block(text) == '{"untagged": 1}'


def test_extract_json_block_prefers_json_over_untagged():
    text = '```\n{"untagged": 1}\n```\n```json\n{"tagged": 2}\n```'
    assert extract_json_block(text) == '{"tagged": 2}'


def test_extract_json_block_missing_raises():
    with pytest.raises(AgentResultError, match="no fenced code block"):
        extract_json_block("just some prose, no block at all")


# --- parse_agent_result ----------------------------------------------------


def test_parse_agent_result_valid():
    result = parse_agent_result(_fenced(_VALID_RESULT))
    assert isinstance(result, AgentResult)
    assert result.bug == "123"
    assert result.status is TriageStatus.triaged
    assert result.proposed_fix.kind is ProposedFixKind.reference
    assert result.suggested_improvements == "Add a cache."


def test_parse_agent_result_no_change_status():
    payload = {**_VALID_RESULT, "status": "no-change"}
    assert parse_agent_result(_fenced(payload)).status is TriageStatus.no_change


def test_parse_agent_result_ignores_extra_fields():
    payload = {**_VALID_RESULT, "unexpected": "ignored"}
    # Extra keys are tolerated; known fields still validated.
    assert parse_agent_result(_fenced(payload)).bug == "123"


def test_parse_agent_result_invalid_status():
    payload = {**_VALID_RESULT, "status": "Bogus"}
    with pytest.raises(AgentResultError, match="validation"):
        parse_agent_result(_fenced(payload))


def test_parse_agent_result_invalid_fix_kind():
    payload = {**_VALID_RESULT, "proposed_fix": {"kind": "magic", "value": ""}}
    with pytest.raises(AgentResultError, match="validation"):
        parse_agent_result(_fenced(payload))


def test_parse_agent_result_missing_required_field():
    payload = {k: v for k, v in _VALID_RESULT.items() if k != "status"}
    with pytest.raises(AgentResultError, match="validation"):
        parse_agent_result(_fenced(payload))


def test_parse_agent_result_garbled_json():
    text = "```json\n{not valid json,,,}\n```"
    with pytest.raises(AgentResultError, match="not valid JSON"):
        parse_agent_result(text)


def test_parse_agent_result_non_object():
    text = "```json\n[1, 2, 3]\n```"
    with pytest.raises(AgentResultError, match="must be an object"):
        parse_agent_result(text)


# --- system prompt ---------------------------------------------------------


def test_load_system_prompt_ships_as_resource():
    prompt = load_system_prompt()
    assert "Role" in prompt
    assert "proposed_fix" in prompt


# --- triage_bugs loop ------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_bugs_success():
    provider = FakeProvider([_fenced(_VALID_RESULT)])
    outcomes = await triage_bugs(provider, [{"number": "123"}], system_prompt="SYS")

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ok
    assert outcome.bug == "123"
    assert outcome.result.status is TriageStatus.triaged
    assert outcome.error is None
    # The payload is forwarded as a JSON user message under the given system prompt.
    assert provider.calls == [("SYS", json.dumps({"number": "123"}, ensure_ascii=False))]


@pytest.mark.asyncio
async def test_triage_bugs_skips_and_continues_on_failure():
    # First bug returns garbage, second returns a valid result.
    provider = FakeProvider(["no json here", _fenced({**_VALID_RESULT, "bug": "456"})])
    payloads = [{"number": "123"}, {"number": "456"}]

    outcomes = await triage_bugs(provider, payloads, system_prompt="SYS")

    assert len(outcomes) == 2
    assert not outcomes[0].ok
    assert outcomes[0].bug == "123"
    assert "no fenced code block" in outcomes[0].error
    assert outcomes[0].raw == "no json here"

    assert outcomes[1].ok
    assert outcomes[1].result.bug == "456"


@pytest.mark.asyncio
async def test_triage_bugs_records_provider_exception():
    class _BoomProvider(FakeProvider):
        async def run(self, system_prompt: str, user_message: str) -> str:
            raise RuntimeError("network down")

    outcomes = await triage_bugs(_BoomProvider(), [{"number": "789"}], system_prompt="S")

    assert len(outcomes) == 1
    assert not outcomes[0].ok
    assert outcomes[0].bug == "789"
    assert "provider error" in outcomes[0].error
    assert "network down" in outcomes[0].error
