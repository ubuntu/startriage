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


async def _get_all_pages(session: aiohttp.ClientSession, url: str) -> list:
    """Fetch all pages of a GitHub paginated endpoint, following Link headers."""
    results: list = []
    next_url: str | None = url
    while next_url:
        try:
            async with session.get(next_url) as resp:
                if resp.status != 200:
                    logging.debug("GitHub HTTP %s: %s", resp.status, next_url)
                    break
                try:
                    page = await resp.json(content_type=None)
                except json.JSONDecodeError as exc:
                    logging.debug("GitHub JSON error fetching %s: %s", next_url, exc)
                    break
                if isinstance(page, list):
                    results.extend(page)
                else:
                    results.append(page)
                # Parse Link header for the next page
                link_header = resp.headers.get("Link", "")
                next_url = _parse_next_link(link_header)
        except aiohttp.ClientError as exc:
            logging.debug("GitHub error fetching %s: %s", next_url, exc)
            break
    return results


def _parse_next_link(link_header: str) -> str | None:
    """Extract the 'next' URL from a GitHub Link response header."""
    for part in (p.strip() for p in link_header.split(",")):
        # Each part looks like: <url>; rel="next"
        if 'rel="next"' in part:
            url_part = part.split(";")[0].strip()
            if url_part.startswith("<") and url_part.endswith(">"):
                return url_part[1:-1]
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
    label: str | None = None,
) -> tuple[list[PullRequest], list[Issue]]:
    """Fetch PRs and Issues for one repo updated within [start, end].

    When *label* is given, only items carrying that label are returned.
    All pages are fetched via the GitHub Link-header pagination protocol.
    """
    base = f"{_GH_API}/repos/{org}/{repo}"
    pulls_url = f"{base}/pulls?state=open&sort=updated&direction=desc&per_page=100"
    issues_url = f"{base}/issues?state=open&sort=updated&direction=desc&per_page=100"
    if label:
        pulls_url += f"&labels={label}"
        issues_url += f"&labels={label}"

    prs_data, issues_data = await asyncio.gather(
        _get_all_pages(session, pulls_url),
        _get_all_pages(session, issues_url),
    )

    prs: list[PullRequest] = []
    for d in prs_data:
        pr = PullRequest.from_api_dict(d)
        if start is None or _in_range(pr.created_at, start, end) or _in_range(pr.updated_at, start, end):
            prs.append(pr)

    issues: list[Issue] = []
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
