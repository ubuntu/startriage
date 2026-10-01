"""GitHub data models for startriage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum, auto


def _login(node: dict | None) -> str | None:
    """Login of an ``author { login }`` node; None for deleted users."""
    return (node or {}).get("login")


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Repo:
    owner: str
    name: str
    url: str


@dataclass
class PullRequest:
    number: int
    title: str
    html_url: str
    repo_url: str
    created_at: datetime | None
    updated_at: datetime | None
    state: str
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    last_edited_at: datetime | None = None
    latest_comment_at: datetime | None = None
    reopened_at: datetime | None = None
    closed_at: datetime | None = None
    author: str | None = None
    latest_comment_author: str | None = None

    @classmethod
    def from_graphql_node(cls, node: dict, repo_url: str) -> PullRequest:
        assignee_nodes = node.get("assignees", {}).get("nodes") or []
        comment_nodes = node.get("comments", {}).get("nodes") or []
        return cls(
            number=node["number"],
            title=node["title"],
            html_url=node["url"],
            repo_url=repo_url,
            created_at=_parse_dt(node.get("createdAt")),
            updated_at=_parse_dt(node.get("updatedAt")),
            last_edited_at=_parse_dt(node.get("lastEditedAt")),
            latest_comment_at=_parse_dt(comment_nodes[-1].get("updatedAt") if comment_nodes else None),
            reopened_at=_parse_dt(
                ((node.get("timelineItems", {}).get("nodes") or [{}])[-1]).get("createdAt")
            ),
            closed_at=_parse_dt(node.get("closedAt")),
            state=node["state"].lower(),
            labels=[lbl["name"] for lbl in node.get("labels", {}).get("nodes") or []],
            assignee=assignee_nodes[0]["login"] if assignee_nodes else None,
            author=_login(node.get("author")),
            latest_comment_author=_login(comment_nodes[-1].get("author")) if comment_nodes else None,
        )


@dataclass
class Issue:
    number: int
    title: str
    html_url: str
    repo_url: str
    created_at: datetime | None
    updated_at: datetime | None
    state: str
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None
    last_edited_at: datetime | None = None
    latest_comment_at: datetime | None = None
    reopened_at: datetime | None = None
    closed_at: datetime | None = None
    author: str | None = None
    latest_comment_author: str | None = None

    @classmethod
    def from_graphql_node(cls, node: dict, repo_url: str) -> Issue:
        assignee_nodes = node.get("assignees", {}).get("nodes") or []
        comment_nodes = node.get("comments", {}).get("nodes") or []
        return cls(
            number=node["number"],
            title=node["title"],
            html_url=node["url"],
            repo_url=repo_url,
            created_at=_parse_dt(node.get("createdAt")),
            updated_at=_parse_dt(node.get("updatedAt")),
            last_edited_at=_parse_dt(node.get("lastEditedAt")),
            latest_comment_at=_parse_dt(comment_nodes[-1].get("updatedAt") if comment_nodes else None),
            reopened_at=_parse_dt(
                ((node.get("timelineItems", {}).get("nodes") or [{}])[-1]).get("createdAt")
            ),
            closed_at=_parse_dt(node.get("closedAt")),
            state=node["state"].lower(),
            labels=[lbl["name"] for lbl in node.get("labels", {}).get("nodes") or []],
            assignee=assignee_nodes[0]["login"] if assignee_nodes else None,
            author=_login(node.get("author")),
            latest_comment_author=_login(comment_nodes[-1].get("author")) if comment_nodes else None,
        )


class GitHubItemType(StrEnum):
    issue = auto()
    pr = auto()


@dataclass
class GithubItemEntry:
    item_type: GitHubItemType
    url: str
    repo: str
    repo_url: str
    item: Issue | PullRequest

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.item.number}"

    @classmethod
    def from_key(cls, key: str) -> GithubItemEntry:
        """Create a stub entry from a persisted 'repo#number' key for display in gone-items lists."""
        repo, num_str = key.rsplit("#", 1)
        number = int(num_str)
        repo_url = f"https://github.com/{repo}"
        url = f"{repo_url}/issues/{number}"
        stub = Issue(
            number=number,
            title="",
            html_url=url,
            repo_url=repo_url,
            created_at=None,
            updated_at=None,
            state="closed",
        )
        return cls(item_type=GitHubItemType.issue, url=url, repo=repo, repo_url=repo_url, item=stub)


@dataclass
class RepoResult:
    repo: str
    prs: list[PullRequest] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    labels: list[str] | None = None

    @property
    def full_name(self) -> str:
        return f"{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}"

    @property
    def had_updates(self) -> bool:
        return bool(self.prs or self.issues)
