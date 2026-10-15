"""Host precheck: route each bug before any model session.

Built from the bug payload, the team's member names and the triage window,
so it is a pure function of its inputs (the series table is fetched by the
caller). Routes:

- ``denied``: private bug, never sent to the model;
- ``skip``: in the window only the team acted, and nobody commented;
- ``update``: the team already handled the bug; review the new activity;
- ``new``: triage from scratch.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from ..sources.launchpad.finder import PACKAGING_TASK_TAGS

UPDATE_STATUSES = frozenset({"Triaged", "In Progress", "Fix Committed", "Fix Released"})
APPORT_KEYS = (
    "ProblemType",
    "DistroRelease",
    "Package",
    "SourcePackage",
    "Architecture",
    "ApportVersion",
    "Uname",
    "ExecutablePath",
)
PATCH_SUFFIXES = (".patch", ".diff", ".debdiff")
PROCESS_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[MIR\]", re.I), "MIR"),
    (re.compile(r"\bFFe\b|feature freeze exception", re.I), "FFe"),
    (
        re.compile(r"\b(please )?(merge|sync)\b.*\bfrom (debian|upstream)\b|\bsync request\b", re.I),
        "merge/sync",
    ),
    (re.compile(r"\bSRU\b", re.I), "SRU"),
    (re.compile(r"\bautopkgtests?\b|\bdep8\b", re.I), "autopkgtest"),
    (re.compile(r"\bFTBFS\b|fails? to build", re.I), "FTBFS"),
]
MAX_NEW_COMMENTS = 5

Window = tuple[datetime, datetime]


class Route(StrEnum):
    denied = "denied"
    skip = "skip"
    update = "update"
    new = "new"


class Event(BaseModel):
    date: str
    person: str
    what: str
    team: bool


class Comment(BaseModel):
    author: str
    date: str
    text: str


class Precheck(BaseModel):
    """What the host knows about a bug before any model session."""

    bug: str
    package: str
    route: Route
    reasons: list[str]
    status: str = ""
    importance: str = ""
    tags: list[str] = []
    assignees: list[str] = []
    reporter: str = ""
    team_filed: bool = False
    expiring: bool = False
    affected_series: list[str] = []
    apport: dict[str, str] = {}
    patches: list[str] = []
    process_hints: list[str] = []
    window_events: list[Event] = []
    last_team_comment: Comment | None = None
    new_comments: list[Comment] = []
    series: dict[str, dict[str, Any]] = {}


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _in(window: Window | None, value: str | None) -> bool:
    when = _date(value)
    return window is not None and when is not None and window[0] <= when <= window[1]


def parse_apport(description: str) -> dict[str, str]:
    """Return the well-known apport fields found in a bug description."""
    found: dict[str, str] = {}
    for line in description.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in APPORT_KEYS and key.strip() not in found:
            found[key.strip()] = value.strip()
    return found


def process_hints(title: str, tags: list[str]) -> list[str]:
    hints = [name for regex, name in PROCESS_HINTS if regex.search(title)]
    hints += [f"tag {tag}" for tag in tags if tag in PACKAGING_TASK_TAGS]
    return hints


def _route(
    payload: dict[str, Any],
    *,
    team: frozenset[str],
    window: Window | None,
    events: list[Event],
    comments: list[Comment],
) -> tuple[Route, list[str]]:
    if payload.get("private"):
        return Route.denied, [f"private bug ({payload.get('information_type') or 'Private'})"]
    if (
        window is not None
        and events
        and all(e.team for e in events)
        and not any(e.what == "comment" for e in events)
    ):
        people = ", ".join(sorted({e.person for e in events}))
        return Route.skip, [f"only team activity in the window ({people}), no new comment"]
    reasons: list[str] = []
    statuses = {payload.get("status")} | {a.get("status") for a in payload.get("affected", [])}
    if handled := sorted(s for s in statuses if s in UPDATE_STATUSES):
        reasons.append(f"status {', '.join(handled)}")
    if payload.get("assignees"):
        reasons.append(f"assigned to {', '.join(payload['assignees'])}")
    before = [c for c in comments if c.author in team and not _in(window, c.date)]
    if before:
        reasons.append(f"{before[-1].author} commented on {before[-1].date[:10]}")
    if verification := [t for t in payload.get("tags", []) if t.startswith("verification-")]:
        reasons.append(f"tag {', '.join(verification)}")
    if payload.get("expiring"):
        reasons.append("about to expire")
    if reasons:
        return Route.update, reasons
    return Route.new, ["not handled by the team yet"]


def build_precheck(
    payload: dict[str, Any],
    *,
    team: frozenset[str] = frozenset(),
    window: Window | None = None,
    series: dict[str, dict[str, Any]] | None = None,
) -> Precheck:
    """Route the bug in ``payload`` and gather the facts the phases start from."""
    reporter = payload.get("reporter") or ""
    comments = [
        Comment(author=c.get("author") or "", date=c.get("date") or "", text=c.get("text") or "")
        for c in payload.get("comments", [])
    ]
    events = [
        Event(date=c.date, person=c.author, what="comment", team=c.author in team)
        for c in comments
        if _in(window, c.date)
    ]
    if _in(window, payload.get("date_created")):
        events.insert(
            0, Event(date=payload["date_created"], person=reporter, what="comment", team=reporter in team)
        )
    events += [
        Event(
            date=a["date"],
            person=a.get("person") or "",
            what=a.get("what") or "",
            team=a.get("person") in team,
        )
        for a in payload.get("activity", [])
        if _in(window, a.get("date"))
    ]
    events.sort(key=lambda e: e.date)
    route, reasons = _route(payload, team=team, window=window, events=events, comments=comments)

    team_indexes = [i for i, c in enumerate(comments) if c.author in team]
    last_team = comments[team_indexes[-1]] if team_indexes else None
    if window is not None:
        new = [c for c in comments if _in(window, c.date)]
    else:
        new = comments[team_indexes[-1] + 1 :] if team_indexes else comments
    attachments = payload.get("attachments", [])
    tags = list(payload.get("tags", []))
    return Precheck(
        bug=str(payload.get("number", "")),
        package=payload.get("package") or "",
        route=route,
        reasons=reasons,
        status=payload.get("status") or "",
        importance=payload.get("importance") or "",
        tags=tags,
        assignees=list(payload.get("assignees", [])),
        reporter=reporter,
        team_filed=reporter in team,
        expiring=bool(payload.get("expiring")),
        affected_series=sorted({a["series"] for a in payload.get("affected", []) if a.get("series")}),
        apport=parse_apport(payload.get("description") or ""),
        patches=[
            a["title"]
            for a in attachments
            if a.get("is_patch") or str(a.get("title", "")).lower().endswith(PATCH_SUFFIXES)
        ],
        process_hints=process_hints(payload.get("title") or "", tags),
        window_events=events,
        last_team_comment=last_team,
        new_comments=new[-MAX_NEW_COMMENTS:],
        series=series or {},
    )
