"""GitHub data models for startriage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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

    @classmethod
    def from_api_dict(cls, d: dict) -> PullRequest:
        return cls(
            number=d["number"],
            title=d["title"],
            html_url=d["html_url"],
            created_at=_parse_dt(d.get("created_at")),
            updated_at=_parse_dt(d.get("updated_at")),
            state=d.get("state", ""),
            labels=[lbl["name"] for lbl in d.get("labels", [])],
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

    @classmethod
    def from_api_dict(cls, d: dict) -> Issue:
        return cls(
            number=d["number"],
            title=d["title"],
            html_url=d["html_url"],
            created_at=_parse_dt(d.get("created_at")),
            updated_at=_parse_dt(d.get("updated_at")),
            state=d.get("state", ""),
            labels=[lbl["name"] for lbl in d.get("labels", [])],
        )
