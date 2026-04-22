"""GitHub data models for startriage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, auto


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


@dataclass
class PullRequest:
    number: int
    title: str
    html_url: str
    created_at: datetime | None
    updated_at: datetime | None
    state: str
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None

    @classmethod
    def from_api_dict(cls, d: dict) -> PullRequest:
        assignee_obj = d.get("assignee")
        return cls(
            number=d["number"],
            title=d["title"],
            html_url=d["html_url"],
            created_at=_parse_dt(d.get("created_at")),
            updated_at=_parse_dt(d.get("updated_at")),
            state=d.get("state", ""),
            labels=[lbl["name"] for lbl in d.get("labels", [])],
            assignee=assignee_obj.get("login") if assignee_obj else None,
        )


@dataclass
class Issue:
    number: int
    title: str
    html_url: str
    created_at: datetime | None
    updated_at: datetime | None
    state: str
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None

    @classmethod
    def from_api_dict(cls, d: dict) -> Issue:
        assignee_obj = d.get("assignee")
        return cls(
            number=d["number"],
            title=d["title"],
            html_url=d["html_url"],
            created_at=_parse_dt(d.get("created_at")),
            updated_at=_parse_dt(d.get("updated_at")),
            state=d.get("state", ""),
            labels=[lbl["name"] for lbl in d.get("labels", [])],
            assignee=assignee_obj.get("login") if assignee_obj else None,
        )


class GitHubItemType(StrEnum):
    issue = auto()
    pr = auto()


@dataclass
class GithubItemEntry:
    item_type: GitHubItemType
    url: str
    repo: str
    item: Issue | PullRequest


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
