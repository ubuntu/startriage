"""GitHub authentication helpers: token resolution and OAuth device flow."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import webbrowser

import aiohttp

from startriage.config import DEFAULT_USER_CONFIG, StarTriageConfig, update_user_config

logger = logging.getLogger(__name__)

_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
# OAuth App client ID for startriage device flow
_GITHUB_OAUTH_CLIENT_ID = "Ov23liTxvpRZsRUKCgXQ"


class GitHubRateLimitError(RuntimeError):
    """Raised when the GitHub API rate limit is exceeded."""

    def __init__(self) -> None:
        super().__init__(
            "GitHub API rate limit exceeded.\n\n"
            "To authenticate and get a higher rate limit, you can:\n"
            "  1. Run 'startriage github login' to authenticate interactively\n"
            f"  2. Set github_token in {DEFAULT_USER_CONFIG} through "
            "`startriage config set --github-token <token>`\n"
            "     - get a personal access token from https://github.com/settings/tokens\n"
            "     - the special token value 'gh' fetches a token from the gh CLI dynamically)\n"
            "  3. Set the GITHUB_TOKEN environment variable\n"
        )


async def _run_github_login(args: argparse.Namespace, _config: StarTriageConfig) -> None:
    token = await github_device_flow_login()
    path = update_user_config(
        {"general": {"github_token": token}},
        config_path=args.config,
        sensitive=True,
    )
    print(f"GitHub token saved to: {path}")


def get_github_token(config_token: str | None = None) -> str | None:
    """Return a GitHub token from env, config, or gh CLI, or None.

    Priority:
    1. GITHUB_TOKEN environment variable
    2. Config token (if "gh", fetch via gh CLI; otherwise use as-is)
    """
    token = os.environ.get(_GITHUB_TOKEN_ENV)
    if token:
        return token

    if config_token is not None:
        if config_token == "gh":
            return _gh_auth_token()
        return config_token

    return None


def _gh_auth_token() -> str | None:
    """Fetch token from gh CLI if available, otherwise None."""
    if shutil.which("gh") is None:
        return None
    logging.info("Fetching GitHub token from gh CLI")
    result = subprocess.run(
        ["gh", "auth", "token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()

    return None


async def github_device_flow_login() -> str:
    """Authenticate via GitHub OAuth Device Flow and return an access token."""
    client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID", _GITHUB_OAUTH_CLIENT_ID)

    async with aiohttp.ClientSession() as session:
        scope = ""  # Empty = public read only; "repo" for private repos
        async with session.post(
            _GITHUB_DEVICE_CODE_URL,
            headers={"Accept": "application/json"},
            data={"client_id": client_id, "scope": scope},
        ) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(f"Device flow initiation failed: {data['error']}")
            device_code = data["device_code"]
            user_code = data["user_code"]
            verification_uri = data["verification_uri"]
            interval = data.get("interval", 5)
            expires_in = data.get("expires_in", 900)

        print(f"Please visit: {verification_uri}")
        print(f"Device code: {user_code}")
        try:
            webbrowser.open(verification_uri)
        except Exception:
            pass

        print("waiting for confirmation...")
        # wait for the token authorization
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < expires_in:
            await asyncio.sleep(interval)
            async with session.post(
                _GITHUB_ACCESS_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            ) as resp:
                token_data = await resp.json()
                if "access_token" in token_data:
                    return token_data["access_token"]
                error = token_data.get("error")
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval += 5
                    continue
                if error == "expired_token":
                    raise RuntimeError("Device flow expired. Please try again.")
                if error == "access_denied":
                    raise RuntimeError("Authorization denied by user.")
                raise RuntimeError(f"Device flow failed: {error}")

        raise RuntimeError("Device flow expired. Please try again.")
