"""GitHub API fetcher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

from startriage.enums import FetchMode

from .auth import GitHubRateLimitError
from .models import Issue, PullRequest, Repo, RepoResult

logger = logging.getLogger(__name__)

_GH_GRAPHQL = "https://api.github.com/graphql"

_ITEM_FIELDS = """\
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url state createdAt updatedAt closedAt lastEditedAt
        labels(first: 20) { nodes { name } }
        assignees(first: 1) { nodes { login } }
        comments(last: 1) { nodes { updatedAt } }
        timelineItems(itemTypes: [REOPENED_EVENT], last: 1) {
          nodes { ... on ReopenedEvent { createdAt } }
        }
      }"""


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
    """Execute a GraphQL query and return the parsed JSON response.

    Retries once on 502 (GitHub transient timeout).
    """
    max_retries = 2
    for attempt in range(max_retries):
        async with session.post(_GH_GRAPHQL, json={"query": query, "variables": variables}) as resp:
            if resp.status == 502 and attempt < max_retries - 1:
                logger.debug("GitHub GraphQL 502, retrying (attempt %d)", attempt + 1)
                await asyncio.sleep(2**attempt)
                continue
            if resp.status != 200:
                text = await resp.text()
                if resp.status == 403 and "rate limit exceeded" in text.lower():
                    raise GitHubRateLimitError()
                raise RuntimeError(f"GitHub GraphQL ({_GH_GRAPHQL}) HTTP {resp.status}: {text[:200]}")
            data = await resp.json(content_type=None)

        if errors := data.get("errors"):
            raise RuntimeError(f"GitHub GraphQL errors: {errors}")

        return data["data"]
    raise RuntimeError("GitHub GraphQL: unreachable")


def _is_actionable(
    item: Issue | PullRequest,
    start: datetime,
    end: datetime,
) -> bool:
    """True if the item has meaningful activity within [start, end]."""
    return (
        _in_range(item.created_at, start, end)
        or _in_range(item.last_edited_at, start, end)
        or _in_range(item.latest_comment_at, start, end)
        or _in_range(item.reopened_at, start, end)
        or _in_range(item.closed_at, start, end)
    )


@dataclass
class _RepoFetchState:
    """Tracks fetch state for one repo across batch iterations."""

    repo: str
    owner: str
    name: str
    gh_repo: Repo
    since_str: str
    issue_states: list[str]
    pr_states: list[str]
    labels: list[str] | None
    mode: FetchMode
    start: datetime | None
    end: datetime | None

    # Accumulated results
    issues: list[Issue] = field(default_factory=list)
    prs: list[PullRequest] = field(default_factory=list)

    # Pagination cursors (None = fetch from start; set after each page)
    issues_cursor: str | None = None
    prs_cursor: str | None = None

    # Whether we still need to fetch more pages
    fetch_issues: bool = True
    fetch_prs: bool = True


def _build_query(states: list[_RepoFetchState], page_size: int = 100) -> str:
    """Build a GraphQL query for all repos that still need fetching.

    Each repo gets aliased fragments for issues and/or PRs depending on
    what still needs pages.
    """
    fragments: list[str] = []
    for i, st in enumerate(states):
        parts: list[str] = []

        # Labels filter only in todo mode (triage shows all activity regardless of labels)
        labels_arg = _labels_arg(st.labels) if st.mode == FetchMode.todo else ""

        if st.fetch_issues:
            issue_states_str = ", ".join(st.issue_states)
            since_arg = f', filterBy: {{since: "{st.since_str}"}}' if st.mode == FetchMode.triage else ""
            cursor_arg = f', after: "{st.issues_cursor}"' if st.issues_cursor else ""
            parts.append(
                f"    issues(first: {page_size}, states: [{issue_states_str}]{labels_arg},"
                f" orderBy: {{field: UPDATED_AT, direction: DESC}}{since_arg}{cursor_arg}) {{\n"
                f"{_ITEM_FIELDS}\n    }}"
            )

        if st.fetch_prs:
            pr_states_str = ", ".join(st.pr_states)
            cursor_arg = f', after: "{st.prs_cursor}"' if st.prs_cursor else ""
            parts.append(
                f"    pullRequests(first: {page_size}, states: [{pr_states_str}]{labels_arg},"
                f" orderBy: {{field: UPDATED_AT, direction: DESC}}{cursor_arg}) {{\n"
                f"{_ITEM_FIELDS}\n    }}"
            )

        fragments.append(
            f'  repo{i}: repository(owner: "{st.owner}", name: "{st.name}") {{\n'
            + "\n".join(parts)
            + "\n  }"
        )

    return "query Batch {\n" + "\n".join(fragments) + "\n}"


def _labels_arg(labels: list[str] | None) -> str:
    if not labels:
        return ""
    return ", labels: [" + ", ".join(f'"{lbl}"' for lbl in labels) + "]"


def _process_connection(
    conn: dict,
    from_node,
    repo_url: str,
    mode: FetchMode,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list, bool, str | None]:
    """Parse a connection response: filter nodes and determine pagination state.

    Returns (items, needs_more_pages, end_cursor).
    """
    nodes = conn.get("nodes") or []
    page_info = conn.get("pageInfo") or {}
    has_next = page_info.get("hasNextPage", False)
    end_cursor = page_info.get("endCursor")

    items: list = []
    exhausted = False
    for node in nodes:
        item = from_node(node, repo_url)
        if mode == FetchMode.triage:
            assert start is not None and end is not None
            if not _in_range(item.updated_at, start, end):
                exhausted = True
                break
            if not _is_actionable(item, start, end):
                continue
        items.append(item)

    needs_more = has_next and not exhausted
    return items, needs_more, end_cursor


async def fetch_repos(
    session: aiohttp.ClientSession,
    repos: list[tuple[str, list[str] | None]],
    mode: FetchMode,
    start: datetime | None,
    end: datetime | None,
) -> list[RepoResult]:
    """Fetch issues and PRs for multiple repos via batched GraphQL queries.

    Uses a single unified query-building approach: the initial fetch and any
    subsequent pagination pages are all handled by the same batch builder,
    looping until no repos need more pages.
    """
    since_str = start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else ""

    # Build initial state for each repo
    all_states: list[_RepoFetchState] = []
    for repo_name, labels in repos:
        owner, name = repo_name.split("/", 1)
        gh_repo = Repo(owner=owner, name=name, url=f"https://github.com/{repo_name}")
        if mode == FetchMode.subscribed:
            issue_states = ["OPEN"]
            pr_states = ["OPEN"]
        else:
            issue_states = ["OPEN", "CLOSED"]
            pr_states = ["OPEN", "CLOSED", "MERGED"]
        all_states.append(
            _RepoFetchState(
                repo=repo_name,
                owner=owner,
                name=name,
                gh_repo=gh_repo,
                since_str=since_str,
                issue_states=issue_states,
                pr_states=pr_states,
                labels=labels,
                mode=mode,
                start=start,
                end=end,
            )
        )

    # Triage queries are heavier (no label filter, all PR states) — use smaller batches.
    # Todo/subscribed has server-side label filtering so can handle more repos per query.
    batch_size = 3 if mode == FetchMode.triage else 10
    for batch_offset in range(0, len(all_states), batch_size):
        batch = all_states[batch_offset : batch_offset + batch_size]

        # Loop until no repo in this batch needs more pages
        while any(st.fetch_issues or st.fetch_prs for st in batch):
            # Only include repos that still need fetching
            active = [st for st in batch if st.fetch_issues or st.fetch_prs]
            if not active:
                break

            logger.debug("building query for batch offset %d, active repos: %s",
                          batch_offset, [st.repo for st in active])
            query = _build_query(active)

            logger.debug("querying github...")
            data = await _graphql(session, query, {})

            logger.debug("processing response...")
            for i, st in enumerate(active):
                repo_data = data.get(f"repo{i}") or {}

                if st.fetch_issues:
                    issues_conn = repo_data.get("issues") or {}
                    items, needs_more, cursor = _process_connection(
                        issues_conn, Issue.from_graphql_node, st.gh_repo.url,
                        st.mode, st.start, st.end,
                    )
                    st.issues.extend(items)
                    st.issues_cursor = cursor
                    st.fetch_issues = needs_more

                if st.fetch_prs:
                    prs_conn = repo_data.get("pullRequests") or {}
                    items, needs_more, cursor = _process_connection(
                        prs_conn, PullRequest.from_graphql_node, st.gh_repo.url,
                        st.mode, st.start, st.end,
                    )
                    st.prs.extend(items)
                    st.prs_cursor = cursor
                    st.fetch_prs = needs_more

    return [
        RepoResult(
            st.repo, st.prs, st.issues, labels=st.labels,
        )
        for st in all_states
    ]
