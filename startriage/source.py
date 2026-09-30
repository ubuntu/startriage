from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Coroutine

from .config import StarTriageConfig, UpdateFilter
from .output import TriageResult


class FetchMode(StrEnum):
    """Which tasks a source fetches."""

    triage = "triage"  # date-range bugs for daily triage
    todo = "todo"  # tag-filtered housekeeping bugs
    subscribed = "subscribed"  # list subscribed bugs


@dataclass(frozen=True)
class TaskFilterOptions:
    team: str
    start: datetime
    end: datetime
    recent_since: datetime
    old_since: datetime
    sources: frozenset[TriageSource]
    show_expiration: bool = True
    update_filter: UpdateFilter | None = None


@dataclass(frozen=True)
class TriageSource:
    name: str
    find: Callable[[StarTriageConfig, TaskFilterOptions, FetchMode], Coroutine[Any, Any, TriageResult]]
