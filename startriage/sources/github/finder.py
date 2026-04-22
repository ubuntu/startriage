"""Async GitHub API fetcher."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.parse
from datetime import date, datetime

import aiohttp

from .models import Issue, PullRequest, RepoResult

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
    repo: str,
    start: date | None,
    end: date | None,
    labels: list[str] | None = None,
) -> RepoResult:
    """Fetch PRs and Issues for one repo updated within [start, end].

    Uses the /issues endpoint exclusively — it returns both issues and PRs and
    supports server-side label filtering (unlike /pulls which ignores labels).
    Multiple labels are ORed: one request is made per label and results are
    deduplicated by number.  When *labels* is empty/None, all open items are
    returned without label filtering.
    """
    base = f"{_GH_API}/repos/{repo}/issues?state=open&sort=updated&direction=desc&per_page=100"

    if labels:
        # Fetch each label separately and deduplicate (GitHub AND-s multiple labels in one request)
        seen: set[int] = set()
        raw: list[dict] = []
        for lbl in labels:
            page_data = await _get_all_pages(session, base + f"&labels={urllib.parse.quote(lbl, safe='')}")
            for item in page_data:
                if item["number"] not in seen:
                    seen.add(item["number"])
                    raw.append(item)
    else:
        raw = await _get_all_pages(session, base)

    prs: list[PullRequest] = []
    issues: list[Issue] = []
    for d in raw:
        if "pull_request" in d:
            pr = PullRequest.from_api_dict(d)
            if start is None or _in_range(pr.created_at, start, end) or _in_range(pr.updated_at, start, end):
                prs.append(pr)
        else:
            issue = Issue.from_api_dict(d)
            if (
                start is None
                or _in_range(issue.created_at, start, end)
                or _in_range(issue.updated_at, start, end)
            ):
                issues.append(issue)

    return RepoResult(repo, prs, issues, labels=labels)
