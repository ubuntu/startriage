"""Source-independent AI triage analysis and recommended action contract.

This module has no dependency on ``startriage.ai`` (or the Copilot SDK), so HTML
rendering, JSON export, and future task/MR runners can consume structured AI
outputs directly without pulling in the AI runtime.

Top-level shape of an :class:`AIRun`:
- ``items``: list of :class:`ItemAnalysis`, one per triaged item ``(source, key)``,
  each carrying its tracker state snapshot, structured analysis, and a list of
  host-derived :class:`RecommendedAction` entries ready for downstream execution.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class AnalysisState(StrEnum):
    analyzed = "analyzed"
    skipped = "skipped"
    denied = "denied"
    failed = "failed"


class ActionKind(StrEnum):
    tracker_update = "tracker_update"
    prepare_fix = "prepare_fix"
    upstream_report = "upstream_report"
    discuss = "discuss"
    none = "none"


class ActionReadiness(StrEnum):
    """Host confidence gate for future automated dispatch."""

    ready = "ready"
    needs_review = "needs_review"
    info_only = "info_only"


class ItemSnapshot(BaseModel):
    """Tracker state captured at triage time, for drift checks before executing actions."""

    model_config = ConfigDict(extra="ignore")

    status: str = ""
    importance: str = ""
    series: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    expiring: bool = False


class WorkItem(BaseModel):
    """One item gathered for AI analysis, carrying its snapshot and raw payload."""

    model_config = ConfigDict(extra="ignore")

    source: str
    key: str
    url: str = ""
    package: str = ""
    title: str = ""
    state: ItemSnapshot = Field(default_factory=ItemSnapshot)
    payload: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def item_id(self) -> tuple[str, str]:
        return (self.source, self.key)


class CodeLocationInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    line: int = 0
    ref: str = ""
    note: str = ""


class RegressionInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suspect_version: str
    previous_version: str
    hunk: str = ""
    is_update: bool = False


class FixCheckInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    state: str
    patches_applied: bool = True
    summary: str = ""


class ProposedFixInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = "none"  # none | reference | diff
    value: str = ""
    unmerged: bool = False


class SRUCandidateInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    series: str
    reason: str


class PhaseSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    phase: str
    state: str
    summary: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    sessions: int = 0


class PhaseCost(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sessions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class RecommendedAction(BaseModel):
    """One host-derived actionable step for an item."""

    model_config = ConfigDict(extra="ignore")

    kind: ActionKind
    label: str
    readiness: ActionReadiness = ActionReadiness.needs_review
    params: dict[str, Any] = Field(default_factory=dict)


class ItemAnalysis(BaseModel):
    """Structured AI analysis and recommended actions for one item."""

    model_config = ConfigDict(extra="ignore")

    source: str
    key: str
    url: str = ""
    package: str = ""
    title: str = ""
    snapshot: ItemSnapshot = Field(default_factory=ItemSnapshot)

    state: AnalysisState
    route: str = ""
    note: str = ""
    error: str = ""

    status: str = ""
    importance: str = ""
    series: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    duplicate_of: str = ""
    analysis: str = ""
    suggested_comment: str = ""
    proposed_fix: ProposedFixInfo = Field(default_factory=ProposedFixInfo)
    fix_applies: dict[str, str] = Field(default_factory=dict)
    fix_checks: dict[str, FixCheckInfo] = Field(default_factory=dict)
    bitesize_verified: bool = False
    locations: list[CodeLocationInfo] = Field(default_factory=list)
    regression: RegressionInfo | None = None
    upstream_url: str = ""
    sru_candidates: list[SRUCandidateInfo] = Field(default_factory=list)
    upstream_suggestion: str = ""
    references: list[str] = Field(default_factory=list)

    phases: list[PhaseSummary] = Field(default_factory=list)
    actions: list[RecommendedAction] = Field(default_factory=list)

    @property
    def item_id(self) -> tuple[str, str]:
        return (self.source, self.key)

    @property
    def has_fix(self) -> bool:
        return self.proposed_fix.kind != "none" and bool(self.proposed_fix.value.strip())

    def to_task_dict(self) -> dict[str, Any]:
        """Self-contained JSON-serializable hand-off payload for one item."""
        return {
            "schema_version": SCHEMA_VERSION,
            **self.model_dump(mode="json", exclude_defaults=True),
            "source": self.source,
            "key": self.key,
            "state": self.state.value,
            "actions": [a.model_dump(mode="json", exclude_defaults=True) for a in self.actions],
        }


class AIRun(BaseModel):
    """Batch output of running AI triage over a set of items."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    model: str = ""
    tools: list[str] = Field(default_factory=list)
    items: list[ItemAnalysis] = Field(default_factory=list)
    totals: dict[str, PhaseCost] = Field(default_factory=dict)

    def by_item(self) -> dict[tuple[str, str], ItemAnalysis]:
        return {item.item_id: item for item in self.items}

    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.input_tokens for c in self.totals.values()),
            sum(c.output_tokens for c in self.totals.values()),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def write_json(self, path: Path) -> Path:
        """Write this run as formatted JSON to ``path``."""
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path
