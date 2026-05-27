"""GitHub API fetcher."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from enum import StrEnum
from typing import cast

import aiohttp

from startriage.enums import FetchMode

from .auth import GitHubRateLimitError
from .models import Issue, PullRequest, Repo, RepoResult

logger = logging.getLogger(__name__)

_GH_GRAPHQL = "https://api.github.com/graphql"

_ISSUES_QUERY = """
query RepoIssues(
  $owner: String!, $name: String!, $since: DateTime!, $states: [IssueState!]!, $cursor: String
) {
  repository(owner: $owner, name: $name) {
    issues(
      first: 100, states: $states, orderBy: {field: UPDATED_AT, direction: DESC},
      filterBy: {since: $since}, after: $cursor
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url state createdAt updatedAt closedAt lastEditedAt
        labels(first: 20) { nodes { name } }
        assignees(first: 1) { nodes { login } }
        comments(last: 1) { nodes { updatedAt } }
        timelineItems(itemTypes: [REOPENED_EVENT], last: 1) { nodes { ... on ReopenedEvent { createdAt } } }
      }
    }
  }
}
"""

_PRS_QUERY = """
query RepoPRs(
  $owner: String!, $name: String!, $states: [PullRequestState!]!, $cursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      first: 100, states: $states, orderBy: {field: UPDATED_AT, direction: DESC},
      after: $cursor
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url state createdAt updatedAt closedAt lastEditedAt
        labels(first: 20) { nodes { name } }
        assignees(first: 1) { nodes { login } }
        comments(last: 1) { nodes { updatedAt } }
        timelineItems(itemTypes: [REOPENED_EVENT], last: 1) { nodes { ... on ReopenedEvent { createdAt } } }
      }
    }
  }
}
"""


class QueryTarget(StrEnum):
    issues = "issues"
    prs = "pullRequests"


def _make_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "startriage/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _in_range(dt: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    if dt is None or start is None or end is None:
        return False
    return start <= dt <= end


async def _graphql(
    session: aiohttp.ClientSession,
    query: str,
    variables: dict,
) -> dict:
    """Execute a GraphQL query and return the parsed JSON response."""
    async with session.post(_GH_GRAPHQL, json={"query": query, "variables": variables}) as resp:
        if resp.status != 200:
            text = await resp.text()
            if resp.status == 403 and "rate limit exceeded" in text.lower():
                raise GitHubRateLimitError()
            raise RuntimeError(f"GitHub GraphQL HTTP {resp.status}: {text[:200]}")
        data = await resp.json(content_type=None)

    if errors := data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {errors}")

    return data["data"]


def _is_actionable(
    item: Issue | PullRequest,
    start: datetime,
    end: datetime,
) -> bool:
    """True if the item has meaningful activity within [start, end].

    An item updated_at within the window but failing all conditions was
    touched only by metadata changes (label, assignee, milestone, …).
    """
    return (
        _in_range(item.created_at, start, end)
        or _in_range(item.last_edited_at, start, end)
        or _in_range(item.latest_comment_at, start, end)
        or _in_range(item.reopened_at, start, end)
        or _in_range(item.closed_at, start, end)
    )


async def _fetch_items(
    session: aiohttp.ClientSession,
    gh_repo: Repo,
    query_target: QueryTarget,
    since_str: str,
    mode: FetchMode,
    start: datetime | None,
    end: datetime | None,
    labels: list[str] | None,
) -> list[Issue] | list[PullRequest]:
    items: list = []
    cursor: str | None = None

    match query_target:
        case QueryTarget.issues:
            logger.debug("Fetching issues for %s/%s", gh_repo.owner, gh_repo.name)
            query = _ISSUES_QUERY
            from_node = Issue.from_graphql_node
        case QueryTarget.prs:
            logger.debug("Fetching PRs for %s/%s", gh_repo.owner, gh_repo.name)
            query = _PRS_QUERY
            from_node = PullRequest.from_graphql_node

        case _:
            raise NotImplementedError

    # In todo mode, closed items can still have the todo label; only subscribed gets OPEN-only.
    # For PRs, also include MERGED to catch closed/merged PRs with todo labels.
    if mode == FetchMode.subscribed:
        states = ["OPEN"]
    else:
        match query_target:
            case QueryTarget.prs:
                states = ["OPEN", "CLOSED", "MERGED"]
            case QueryTarget.issues:
                states = ["OPEN", "CLOSED"]
            case _:
                raise NotImplementedError

    while True:
        variables: dict = {"owner": gh_repo.owner, "name": gh_repo.name, "states": states, "cursor": cursor}
        if query_target == QueryTarget.issues:
            variables["since"] = since_str
        data = await _graphql(session, query, variables)
        conn = (data.get("repository") or {}).get(query_target.value) or {}
        for node in conn.get("nodes") or []:
            item = from_node(node, gh_repo.url)
            if mode == FetchMode.triage:
                assert start is not None and end is not None
                if not _in_range(item.updated_at, start, end):
                    return items  # sorted DESC; nothing further in range
                if not _is_actionable(item, start, end):
                    logging.debug(
                        "Skipping %s/%s#%s: metadata-only update",
                        gh_repo.owner,
                        gh_repo.name,
                        item.number,
                    )
                    continue
            if labels is None or any(lbl in item.labels for lbl in labels):
                items.append(item)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info["endCursor"]
    return items


async def fetch_repo(
    session: aiohttp.ClientSession,
    mode: FetchMode,
    repo: str,
    start: datetime | None,
    end: datetime | None,
    labels: list[str] | None = None,
) -> RepoResult:
    """Fetch PRs and Issues for one repo via GraphQL, concurrently."""
    owner, name = repo.split("/", 1)
    gh_repo = Repo(owner=owner, name=name, url=f"https://github.com/{repo}")
    since_str = start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else "1970-01-01T00:00:00Z"

    issues, prs = await asyncio.gather(
        _fetch_items(session, gh_repo, QueryTarget.issues, since_str, mode, start, end, labels),
        _fetch_items(session, gh_repo, QueryTarget.prs, since_str, mode, start, end, labels),
    )
    return RepoResult(repo, cast(list[PullRequest], prs), cast(list[Issue], issues), labels=labels)
