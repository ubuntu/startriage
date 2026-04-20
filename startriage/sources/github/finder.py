"""Async GitHub API fetcher."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import date, datetime

import aiohttp

from .models import Issue, PullRequest

_GH_API = "https://api.github.com"
_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"


def get_github_token() -> str | None:
    """Return a GitHub token from gh CLI or GITHUB_TOKEN env var, or None."""
    token = os.environ.get(_GITHUB_TOKEN_ENV)
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _make_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "startriage/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get_json(session: aiohttp.ClientSession, url: str) -> list | dict | None:
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            logging.debug("GitHub HTTP %s: %s", resp.status, url)
            return None
    except (aiohttp.ClientError, json.JSONDecodeError) as exc:
        logging.debug("GitHub error fetching %s: %s", url, exc)
        return None


def _in_range(dt: datetime | None, start: date | None, end: date | None) -> bool:
    if dt is None or start is None or end is None:
        return False
    d = dt.date()
    return start <= d <= end


async def fetch_repo(
    session: aiohttp.ClientSession,
    org: str,
    repo: str,
    start: date | None,
    end: date | None,
) -> tuple[list[PullRequest], list[Issue]]:
    """Fetch PRs and Issues for one repo updated within [start, end]."""
    base = f"{_GH_API}/repos/{org}/{repo}"

    prs_data, issues_data = await asyncio.gather(
        _get_json(session, f"{base}/pulls?state=open&sort=updated&direction=desc&per_page=100"),
        _get_json(session, f"{base}/issues?state=open&sort=updated&direction=desc&per_page=100"),
    )

    prs: list[PullRequest] = []
    if prs_data and isinstance(prs_data, list):
        for d in prs_data:
            pr = PullRequest.from_api_dict(d)
            if start is None or _in_range(pr.created_at, start, end) or _in_range(pr.updated_at, start, end):
                prs.append(pr)

    issues: list[Issue] = []
    if issues_data and isinstance(issues_data, list):
        for d in issues_data:
            if "pull_request" in d:
                continue  # GH issues endpoint also returns PRs
            issue = Issue.from_api_dict(d)
            if (
                start is None
                or _in_range(issue.created_at, start, end)
                or _in_range(issue.updated_at, start, end)
            ):
                issues.append(issue)

    return prs, issues
