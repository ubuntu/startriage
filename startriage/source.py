from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Coroutine

from .config import StarTriageConfig
from .enums import FetchMode, UpdateFilter
from .output import TriageResult


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
