"""Tests for startriage.sources.github.finder and auth."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from startriage.sources.github.auth import (
    GitHubRateLimitError,
    get_github_token,
    github_device_flow_login,
)
from startriage.sources.github.finder import _graphql


class TestGetGitHubToken:
    def test_env_var_takes_priority(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env_token")
        assert get_github_token() == "env_token"
        assert get_github_token(config_token="config_token") == "env_token"

    def test_config_token_used_when_no_env(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert get_github_token(config_token="config_token") == "config_token"

    def test_config_token_gh_invokes_gh_cli(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("startriage.sources.github.auth._gh_auth_token", return_value="gh_token"):
            assert get_github_token(config_token="gh") == "gh_token"

    def test_none_when_no_sources_available(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("startriage.sources.github.auth._gh_auth_token", return_value=None):
            assert get_github_token() is None

    def test_gh_auth_token_with_gh_installed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("startriage.sources.github.auth.shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "startriage.sources.github.auth.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="gh_cli_token\n"),
            ) as mock_run:
                result = get_github_token(config_token="gh")
                assert result == "gh_cli_token"
                mock_run.assert_called_once_with(
                    ["gh", "auth", "token"],
                    capture_output=True,
                    text=True,
                    check=False,
                )

    def test_gh_auth_token_without_gh_installed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("startriage.sources.github.auth.shutil.which", return_value=None):
            assert get_github_token(config_token="gh") is None


@pytest.mark.asyncio
class TestGraphQL:
    async def test_rate_limit_403_raises_custom_error(self):
        session = MagicMock()
        response = AsyncMock()
        response.status = 403
        response.text = AsyncMock(return_value=json.dumps({"message": "API rate limit exceeded for 1.2.3.4"}))
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(GitHubRateLimitError):
            await _graphql(session, "query {}", {})

    async def test_other_403_raises_runtime_error(self):
        session = MagicMock()
        response = AsyncMock()
        response.status = 403
        response.text = AsyncMock(return_value="Forbidden")
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await _graphql(session, "query {}", {})

    async def test_non_200_raises_runtime_error(self):
        session = MagicMock()
        response = AsyncMock()
        response.status = 500
        response.text = AsyncMock(return_value="Internal Server Error")
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError, match="HTTP 500"):
            await _graphql(session, "query {}", {})


@pytest.mark.asyncio
class TestDeviceFlow:
    async def test_device_flow_success(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test_client_id")

        device_code_resp = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 900,
        }
        token_resp = {"access_token": "gho_testtoken"}

        post_responses = [device_code_resp, token_resp]
        call_count = 0

        def _make_response(data):
            response = AsyncMock()
            response.json = AsyncMock(return_value=data)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            return response

        def mock_post(*args, **kwargs):
            nonlocal call_count
            response = _make_response(post_responses[call_count])
            call_count += 1
            return response

        session = AsyncMock()
        session.post = mock_post
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            with patch("startriage.sources.github.auth.webbrowser.open"):
                token = await github_device_flow_login()
                assert token == "gho_testtoken"

    async def test_device_flow_polling(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test_client_id")

        device_code_resp = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 900,
        }
        pending_resp = {"error": "authorization_pending"}
        token_resp = {"access_token": "gho_testtoken"}

        post_responses = [device_code_resp, pending_resp, token_resp]
        call_count = 0

        def _make_response(data):
            response = AsyncMock()
            response.json = AsyncMock(return_value=data)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            return response

        def mock_post(*args, **kwargs):
            nonlocal call_count
            response = _make_response(post_responses[call_count])
            call_count += 1
            return response

        session = AsyncMock()
        session.post = mock_post
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            with patch("startriage.sources.github.auth.webbrowser.open"):
                with patch("asyncio.sleep", new=AsyncMock()):
                    token = await github_device_flow_login()
                    assert token == "gho_testtoken"

    async def test_device_flow_expired(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test_client_id")

        device_code_resp = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 2,
        }
        pending_resp = {"error": "authorization_pending"}

        post_responses = [device_code_resp, pending_resp]
        call_count = 0

        def _make_response(data):
            response = AsyncMock()
            response.json = AsyncMock(return_value=data)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            return response

        def mock_post(*args, **kwargs):
            nonlocal call_count
            response = _make_response(post_responses[min(call_count, len(post_responses) - 1)])
            call_count += 1
            return response

        session = AsyncMock()
        session.post = mock_post
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            with patch("startriage.sources.github.auth.webbrowser.open"):
                with patch("asyncio.sleep", new=AsyncMock()):
                    with pytest.raises(RuntimeError, match="expired"):
                        await github_device_flow_login()

    async def test_device_flow_access_denied(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test_client_id")

        device_code_resp = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 1,
            "expires_in": 900,
        }
        denied_resp = {"error": "access_denied"}

        post_responses = [device_code_resp, denied_resp]
        call_count = 0

        def _make_response(data):
            response = AsyncMock()
            response.json = AsyncMock(return_value=data)
            response.__aenter__ = AsyncMock(return_value=response)
            response.__aexit__ = AsyncMock(return_value=False)
            return response

        def mock_post(*args, **kwargs):
            nonlocal call_count
            response = _make_response(post_responses[call_count])
            call_count += 1
            return response

        session = AsyncMock()
        session.post = mock_post
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            with patch("startriage.sources.github.auth.webbrowser.open"):
                with patch("asyncio.sleep", new=AsyncMock()):
                    with pytest.raises(RuntimeError, match="denied"):
                        await github_device_flow_login()
