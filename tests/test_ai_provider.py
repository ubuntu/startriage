"""Tests for the AI provider layer (selection, kwargs, fake round-trip)."""

from __future__ import annotations

import pytest

from startriage.ai import (
    CopilotProvider,
    FakeProvider,
    FakeTurn,
    build_client_kwargs,
    build_provider,
    build_session_kwargs,
)
from startriage.ai.provider import SessionSpec, session_options
from startriage.ai.supervisor import OVER_BUDGET
from startriage.ai.tools import ToolSpec
from startriage.config import AIConfig, AIConfigError, AIProvider


@pytest.fixture(autouse=True)
def _clear_ai_env(monkeypatch):
    for var in (
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "STARTRIAGE_AI_OPENROUTER_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _copilot(**overrides) -> AIConfig:
    """A minimally complete Copilot config (provider/model have no defaults)."""
    return AIConfig(provider=AIProvider.copilot, model="a-copilot-model", **overrides)


def test_copilot_token_authenticates_the_client_not_the_session(monkeypatch):
    assert build_client_kwargs(_copilot(github_token="github_pat_abc")) == {"github_token": "github_pat_abc"}
    assert build_session_kwargs(_copilot(github_token="github_pat_abc")) == {}
    # No config token and no env var: the SDK is left to read the env itself.
    assert build_client_kwargs(_copilot()) == {}
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "env_token")
    assert build_client_kwargs(_copilot()) == {"github_token": "env_token"}


def test_openrouter_key_travels_on_the_session():
    cfg = AIConfig(
        provider=AIProvider.openrouter,
        model="anthropic/claude-3.5",
        openrouter_api_key="sk-or-1",
        openrouter_base_url="https://example.test/v1",
    )
    assert build_client_kwargs(cfg) == {}
    assert build_session_kwargs(cfg) == {
        "provider": {"type": "openai", "base_url": "https://example.test/v1", "api_key": "sk-or-1"}
    }


def test_build_provider_returns_copilot_provider():
    provider = build_provider(_copilot(github_token="github_pat_abc"))
    assert isinstance(provider, CopilotProvider)
    assert provider.model == "a-copilot-model"


def test_build_provider_missing_copilot_credential():
    with pytest.raises(AIConfigError, match="No Copilot credential"):
        build_provider(_copilot())


def test_build_provider_unconfigured_backend():
    with pytest.raises(AIConfigError, match=r"\[ai\] provider and model"):
        build_provider(AIConfig(provider=AIProvider.copilot, github_token="t"))


def test_build_provider_missing_openrouter_credential():
    with pytest.raises(AIConfigError, match="No OpenRouter API key"):
        build_provider(AIConfig(provider=AIProvider.openrouter, model="m"))


async def _echo(args: dict) -> str:
    return f"echo {args}"


def test_session_options_expose_only_our_tools(tmp_path):
    tools = [
        ToolSpec("lp_get_bug", "d", {"type": "object"}, _echo),
        ToolSpec("submit_result", "d", {}, _echo),
    ]
    spec = SessionSpec(system_prompt="SYS", task="T", tools=tools, working_directory=tmp_path)
    options = session_options(spec, _copilot())
    assert options["available_tools"] == ["lp_get_bug", "submit_result"]
    assert options["system_message"] == {"mode": "replace", "content": "SYS"}
    assert options["working_directory"] == str(tmp_path)
    assert options["model"] == "a-copilot-model"
    assert options["skip_custom_instructions"] is True


@pytest.mark.asyncio
async def test_fake_provider_applies_the_spec_call_budget():
    tool = ToolSpec("echo", "d", {}, _echo)
    calls = [("echo", {"a": 1}), ("echo", {"a": 2})]
    provider = FakeProvider([[FakeTurn(calls=calls, text="done")]])
    await provider.run(SessionSpec(system_prompt="S", task="T", tools=[tool], max_calls=1))
    assert [(args, ok) for _, args, _, ok in provider.calls] == [({"a": 1}, True), ({"a": 2}, False)]
    assert provider.calls[1][2] == OVER_BUDGET
