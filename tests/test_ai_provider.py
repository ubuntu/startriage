"""Tests for the AI provider layer (selection, kwargs, fake round-trip)."""

from __future__ import annotations

import pytest

from startriage.ai import (
    CopilotProvider,
    FakeProvider,
    build_client_kwargs,
    build_permission_handler,
    build_provider,
    build_session_kwargs,
)
from startriage.config import AIConfig, AIConfigError
from startriage.enums import AIPermission, AIProvider


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


def test_build_session_kwargs_copilot_with_token():
    cfg = AIConfig(github_token="github_pat_abc")
    # The Copilot token authenticates the client, not the session.
    assert build_client_kwargs(cfg) == {"github_token": "github_pat_abc"}
    assert build_session_kwargs(cfg) == {}


def test_build_session_kwargs_copilot_without_token():
    # No config token and no env var -> SDK is left to read the env itself.
    assert build_client_kwargs(AIConfig()) == {}
    assert build_session_kwargs(AIConfig()) == {}


def test_build_session_kwargs_copilot_token_from_env(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "env_token")
    assert build_client_kwargs(AIConfig()) == {"github_token": "env_token"}
    assert build_session_kwargs(AIConfig()) == {}


def test_build_session_kwargs_openrouter():
    cfg = AIConfig(
        provider=AIProvider.openrouter,
        model="anthropic/claude-3.5",
        openrouter_api_key="sk-or-1",
    )
    # BYOK travels on the session; the client needs no auth.
    assert build_client_kwargs(cfg) == {}
    assert build_session_kwargs(cfg) == {
        "provider": {
            "type": "openai",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-1",
        }
    }


def test_build_session_kwargs_openrouter_custom_base_url():
    cfg = AIConfig(
        provider=AIProvider.openrouter,
        openrouter_api_key="sk-or-2",
        openrouter_base_url="https://example.test/v1",
    )
    assert build_session_kwargs(cfg)["provider"]["base_url"] == "https://example.test/v1"


def test_build_provider_returns_copilot_provider():
    provider = build_provider(AIConfig(github_token="github_pat_abc"), AIPermission.restricted)
    assert isinstance(provider, CopilotProvider)
    assert provider.model == "claude-opus-4.8"


def test_build_provider_openrouter_uses_configured_model():
    cfg = AIConfig(
        provider=AIProvider.openrouter,
        model="anthropic/claude-3.5",
        openrouter_api_key="sk-or-1",
    )
    assert build_provider(cfg, AIPermission.restricted).model == "anthropic/claude-3.5"


def test_build_provider_threads_permission():
    provider = build_provider(AIConfig(github_token="github_pat_abc"), AIPermission.full)
    assert isinstance(provider, CopilotProvider)
    assert provider._permission is AIPermission.full


def test_build_provider_missing_copilot_credential():
    with pytest.raises(AIConfigError, match="Copilot"):
        build_provider(AIConfig(), AIPermission.restricted)


def test_build_provider_missing_openrouter_credential():
    with pytest.raises(AIConfigError, match="OpenRouter"):
        build_provider(AIConfig(provider=AIProvider.openrouter), AIPermission.restricted)


def test_build_permission_handler_full_approves_all():
    pytest.importorskip("copilot")
    from copilot.session import PermissionHandler  # ty: ignore[unresolved-import]

    assert build_permission_handler(AIPermission.full) is PermissionHandler.approve_all


def test_build_permission_handler_restricted_and_ask_are_callables():
    pytest.importorskip("copilot")

    assert callable(build_permission_handler(AIPermission.restricted))
    assert callable(build_permission_handler(AIPermission.ask))


@pytest.mark.asyncio
async def test_fake_provider_round_trip_queued_responses():
    provider = FakeProvider(["first", "second"], model="fake-x")
    assert provider.model == "fake-x"

    assert await provider.run("sys", "bug-1") == "first"
    assert await provider.run("sys", "bug-2") == "second"
    # Queue drained -> default response.
    assert await provider.run("sys", "bug-3") == ""

    assert provider.calls == [
        ("sys", "bug-1"),
        ("sys", "bug-2"),
        ("sys", "bug-3"),
    ]


@pytest.mark.asyncio
async def test_fake_provider_default_response():
    provider = FakeProvider(default_response="canned")
    assert await provider.run("sys", "anything") == "canned"
