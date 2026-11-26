"""The result of a bug, built by the host from what the phases submitted."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..analysis import (
    ActionKind,
    ActionReadiness,
    AnalysisState,
    CodeLocationInfo,
    FixCheckInfo,
    ItemAnalysis,
    ItemSnapshot,
    PhaseSummary,
    ProposedFixInfo,
    RecommendedAction,
    RegressionInfo,
    SRUCandidateInfo,
    WorkItem,
)
from .agent import BugOutcome
from .archive import Regression
from .checks import bitesize, describe
from .contract import AgentResult, Importance, ProposedFix, ProposedFixKind, TriageStatus
from .phases import (
    EARLY_EXIT,
    Analysis,
    Assessment,
    FixProposal,
    PackageContext,
    Report,
    UpdateReview,
    ValidateResult,
)
from .precheck import Precheck
from .tools.patch import ApplyState, PatchCheck


def _short_title(payload: dict[str, Any], v: ValidateResult | None = None) -> str:
    return (v.short_title if v else "") or payload.get("short_title") or ""


def from_review(payload: dict[str, Any], pre: Precheck, review: UpdateReview) -> AgentResult:
    return AgentResult(
        bug=pre.bug,
        package=pre.package,
        short_title=_short_title(payload),
        status=review.status or TriageStatus.no_change,
        tags=review.tags,
        analysis=f"**Update review ({review.action.value}):** {review.reason}",
        suggested_comment=review.comment,
    )


def from_verdict(payload: dict[str, Any], pre: Precheck, v: ValidateResult) -> AgentResult:
    return AgentResult(
        bug=pre.bug,
        package=pre.package,
        short_title=_short_title(payload, v),
        status=EARLY_EXIT[v.verdict],
        # Importance is for bugs someone will work on, not for early exits.
        analysis=f"**Verdict: {v.verdict.value}.** {v.reason}",
        suggested_comment=v.comment,
    )


@dataclass(frozen=True)
class PhaseOutputs:
    """What the post-validation phases produced for one bug."""

    assessment: Assessment | None = None
    regression: Regression = field(default_factory=lambda: Regression([]))
    analysis: Analysis | None = None
    fix: FixProposal | None = None
    fix_applies: dict[str, PatchCheck] = field(default_factory=dict)
    context: PackageContext | None = None


def from_phases(
    payload: dict[str, Any],
    pre: Precheck,
    v: ValidateResult,
    report: Report | None,
    outputs: PhaseOutputs,
    *,
    discuss_tag: str | None = None,
) -> AgentResult:
    """The report, with the fix and how it applies; or, if write-report failed, what the phases found."""
    assessment = outputs.assessment
    regression = outputs.regression
    analysis = outputs.analysis
    fix = outputs.fix
    fix_applies = outputs.fix_applies
    context = outputs.context
    resolved_package = (context.package if context else "") or pre.package
    upstream = context.upstream if context else None
    fix_checks = {
        ref: FixCheckInfo(
            ref=ref,
            state=c.state.value,
            patches_applied=c.patches_applied,
            summary=describe(c),
        )
        for ref, c in fix_applies.items()
    }
    locations = [
        CodeLocationInfo(path=loc.path, line=loc.line, ref=loc.ref, note=loc.note)
        for loc in (analysis.locations if analysis else [])
    ]
    confirmed_reg = regression.confirmed
    regression_info = (
        RegressionInfo(
            suspect_version=confirmed_reg.suspect_version,
            previous_version=confirmed_reg.previous_version,
            hunk=confirmed_reg.hunk,
            is_update=regression.update is not None,
        )
        if confirmed_reg is not None
        else None
    )
    upstream_url = (upstream.repository or upstream.bug_database or upstream.homepage) if upstream else ""
    base: dict[str, Any] = {
        "bug": pre.bug,
        "package": resolved_package,
        "short_title": _short_title(payload, v),
        "proposed_fix": fix.proposed_fix if fix else ProposedFix(kind=ProposedFixKind.none),
        "fix_applies": {ref: describe(c) for ref, c in fix_applies.items()},
        "fix_checks": fix_checks,
        "fix_unmerged": bool(fix.unmerged) if fix else False,
        "bitesize_verified": bool(
            fix is not None and fix.proposed_fix.kind is not ProposedFixKind.none and bitesize(fix_applies)
        ),
        "locations": locations,
        "regression": regression_info,
        "upstream_url": upstream_url,
    }
    fix_references = fix.references if fix else []
    if report is not None:
        references = list(dict.fromkeys([*report.references, *fix_references]))
        return AgentResult.model_validate(base | report.model_dump() | {"references": references})
    parts = [f"**Verdict: {v.verdict.value}.** {v.reason}"]
    if assessment is not None:
        parts.append(assessment.summary)
    if regression.result is not None:
        parts.append(regression.result.summary)
    if analysis is not None:
        parts += [analysis.summary, f"Root cause ({analysis.confidence.value}): {analysis.root_cause}"]
    if fix is not None:
        parts.append(fix.summary)
    parts.append("_The report phase failed; this result was assembled by the host._")
    upstream = [s for s in (fix and fix.upstream_suggestion, analysis and analysis.upstream_suggestion) if s]
    return AgentResult.model_validate(
        base
        | {
            "status": TriageStatus.no_change,
            "importance": v.importance,
            "tags": [discuss_tag] if discuss_tag else [],
            "duplicate_of": assessment.duplicate_of if assessment else "",
            "analysis": "\n\n".join(p for p in parts if p),
            "references": fix_references,
            "sru_candidates": assessment.sru_candidates if assessment else [],
            "upstream_suggestion": upstream[0] if upstream else "",
        }
    )


_CLEAN_APPLY = frozenset({ApplyState.clean.value, ApplyState.offset.value})
_TERMINAL_STATUSES = frozenset(
    {
        TriageStatus.invalid,
        TriageStatus.incomplete,
        TriageStatus.fix_released,
        TriageStatus.wont_fix,
        TriageStatus.duplicate,
    }
)


def derive_actions(result: AgentResult, *, discuss_tag: str | None = None) -> list[RecommendedAction]:
    """Derive host-typed recommended actions from a verified :class:`AgentResult`."""
    actions: list[RecommendedAction] = []
    has_discuss_tag = bool(discuss_tag and discuss_tag in result.tags)

    update_params: dict[str, Any] = {}
    if result.status is not TriageStatus.no_change:
        update_params["status"] = result.status.value
    if result.importance is not Importance.undecided:
        update_params["importance"] = result.importance.value
    if result.series:
        update_params["series"] = {s: st.value for s, st in result.series.items()}
    if result.tags:
        update_params["tags"] = list(result.tags)
    if result.duplicate_of:
        update_params["duplicate_of"] = result.duplicate_of.lstrip("#")
    if result.suggested_comment.strip():
        update_params["comment"] = result.suggested_comment.strip()

    if update_params:
        parts: list[str] = []
        if "status" in update_params:
            status_label = update_params["status"]
            if "duplicate_of" in update_params:
                status_label += f" (#{update_params['duplicate_of']})"
            parts.append(status_label)
        if "tags" in update_params:
            parts.append(", ".join(update_params["tags"]))
        if "comment" in update_params:
            parts.append("post comment")
        label = "Update bug: " + " · ".join(parts) if parts else "Update bug"
        readiness = (
            ActionReadiness.needs_review
            if has_discuss_tag
            else ActionReadiness.ready
            if result.status in (TriageStatus.duplicate, TriageStatus.fix_released)
            else ActionReadiness.needs_review
        )
        actions.append(
            RecommendedAction(
                kind=ActionKind.tracker_update,
                label=label,
                readiness=readiness,
                params=update_params,
            )
        )

    fix_action = _derive_fix_action(result)
    if fix_action is not None:
        actions.append(fix_action)

    if result.upstream_suggestion.strip():
        upstream_params: dict[str, Any] = {"suggestion": result.upstream_suggestion.strip()}
        if result.upstream_url:
            upstream_params["upstream_url"] = result.upstream_url
        if result.references:
            upstream_params["references"] = list(result.references)
        actions.append(
            RecommendedAction(
                kind=ActionKind.upstream_report,
                label="Follow up upstream",
                readiness=ActionReadiness.needs_review,
                params=upstream_params,
            )
        )

    if not actions:
        actions.append(
            RecommendedAction(
                kind=ActionKind.none,
                label="No action needed",
                readiness=ActionReadiness.info_only,
            )
        )

    return actions


def _derive_fix_action(result: AgentResult) -> RecommendedAction | None:
    """Build a ``prepare_fix`` action when a fix, confirmed regression, or verified root cause exists."""
    if result.status in _TERMINAL_STATUSES:
        return None
    has_fix = result.proposed_fix.kind is not ProposedFixKind.none and bool(result.proposed_fix.value.strip())
    if not (has_fix or result.regression is not None or result.locations):
        return None

    target_series = [
        ref for ref, check in result.fix_checks.items() if check.state != ApplyState.applied.value
    ]
    if not target_series:
        target_series = [
            series
            for series, status in result.series.items()
            if status not in (TriageStatus.fix_released, TriageStatus.wont_fix, TriageStatus.invalid)
        ]
    for sru in result.sru_candidates:
        if sru.series not in target_series:
            target_series.append(sru.series)

    if result.proposed_fix.kind is ProposedFixKind.reference and has_fix:
        strategy = "upstream_reference"
    elif result.regression is not None and not has_fix:
        strategy = "revert_regression"
    elif result.proposed_fix.kind is ProposedFixKind.diff and has_fix:
        strategy = "proposed_diff"
    else:
        strategy = "from_root_cause"

    params: dict[str, Any] = {
        "package": result.package,
        "strategy": strategy,
    }
    if target_series:
        params["target_series"] = target_series
    if has_fix:
        params["proposed_fix"] = {
            "kind": result.proposed_fix.kind.value,
            "value": result.proposed_fix.value,
            "unmerged": result.fix_unmerged,
        }
    if result.fix_checks:
        params["fix_applies"] = {
            ref: check.model_dump(mode="json") for ref, check in result.fix_checks.items()
        }
    if result.bitesize_verified:
        params["bitesize_verified"] = True
    if result.locations:
        params["locations"] = [loc.model_dump(mode="json") for loc in result.locations]
    if result.regression is not None:
        params["regression"] = result.regression.model_dump(mode="json")
    if result.sru_candidates:
        params["sru_candidates"] = [c.model_dump(mode="json") for c in result.sru_candidates]
    if result.upstream_url:
        params["upstream_url"] = result.upstream_url
    if result.references:
        params["references"] = list(result.references)

    checks = list(result.fix_checks.values())
    all_clean = (
        has_fix
        and bool(checks)
        and all(c.patches_applied for c in checks)
        and any(c.state in _CLEAN_APPLY for c in checks)
        and all(c.state in _CLEAN_APPLY | {ApplyState.applied.value} for c in checks)
    )
    readiness = (
        ActionReadiness.ready if all_clean and not result.fix_unmerged else ActionReadiness.needs_review
    )

    series_note = f" ({', '.join(target_series)})" if target_series else ""
    pkg_note = f" {result.package}" if result.package else ""
    label = f"Prepare fix:{pkg_note}{series_note}"
    return RecommendedAction(
        kind=ActionKind.prepare_fix,
        label=label,
        readiness=readiness,
        params=params,
    )


def to_item_analysis(
    outcome: BugOutcome,
    item: WorkItem | None = None,
    *,
    discuss_tag: str | None = None,
) -> ItemAnalysis:
    """Convert a pipeline :class:`BugOutcome` (+ optional :class:`WorkItem`) into :class:`ItemAnalysis`."""
    source = item.source if item else "launchpad"
    key = item.key if item else outcome.bug
    url = item.url if item else (f"https://bugs.launchpad.net/bugs/{outcome.bug}" if outcome.bug else "")
    snapshot = item.state if item else ItemSnapshot()
    phases = [
        PhaseSummary(
            phase=p.phase,
            state=p.state.value,
            summary=p.summary,
            tool_calls=list(p.tool_calls),
            input_tokens=p.input_tokens,
            output_tokens=p.output_tokens,
            sessions=p.sessions,
        )
        for p in outcome.phases
    ]

    if outcome.result is not None:
        r = outcome.result
        return ItemAnalysis(
            source=source,
            key=key,
            url=url,
            package=r.package or (item.package if item else ""),
            title=r.short_title or (item.title if item else ""),
            snapshot=snapshot,
            state=AnalysisState.analyzed,
            route=outcome.route,
            status=r.status.value,
            importance=r.importance.value,
            series={name: st.value for name, st in r.series.items()},
            tags=list(r.tags),
            duplicate_of=r.duplicate_of.lstrip("#"),
            analysis=r.analysis,
            suggested_comment=r.suggested_comment,
            proposed_fix=ProposedFixInfo(
                kind=r.proposed_fix.kind.value,
                value=r.proposed_fix.value,
                unmerged=r.fix_unmerged,
            ),
            fix_applies=dict(r.fix_applies),
            fix_checks=dict(r.fix_checks),
            bitesize_verified=r.bitesize_verified,
            locations=list(r.locations),
            regression=r.regression,
            upstream_url=r.upstream_url,
            sru_candidates=[SRUCandidateInfo(series=c.series, reason=c.reason) for c in r.sru_candidates],
            upstream_suggestion=r.upstream_suggestion,
            references=list(r.references),
            phases=phases,
            actions=derive_actions(r, discuss_tag=discuss_tag),
        )

    if outcome.error is not None:
        return ItemAnalysis(
            source=source,
            key=key,
            url=url,
            package=item.package if item else "",
            title=item.title if item else "",
            snapshot=snapshot,
            state=AnalysisState.failed,
            route=outcome.route,
            error=outcome.error,
            phases=phases,
            actions=[
                RecommendedAction(
                    kind=ActionKind.discuss,
                    label="Manual triage (AI failed)",
                    readiness=ActionReadiness.info_only,
                    params={"reason": outcome.error},
                )
            ],
        )

    state = AnalysisState.denied if outcome.route == "denied" else AnalysisState.skipped
    return ItemAnalysis(
        source=source,
        key=key,
        url=url,
        package=item.package if item else "",
        title=item.title if item else "",
        snapshot=snapshot,
        state=state,
        route=outcome.route,
        note=outcome.note,
        phases=phases,
        actions=[
            RecommendedAction(
                kind=ActionKind.none,
                label="Manual triage (private)" if state is AnalysisState.denied else "No action needed",
                readiness=ActionReadiness.info_only,
                params={"reason": outcome.note},
            )
        ],
    )
