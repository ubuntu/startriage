"""The result of a bug, built by the host from what the phases submitted."""

from __future__ import annotations

from typing import Any

from .checks import describe
from .contract import AgentResult, ProposedFix, ProposedFixKind, TriageStatus
from .phases import (
    EARLY_EXIT,
    Analysis,
    Assessment,
    FixProposal,
    RegressionCheck,
    Report,
    UpdateReview,
    ValidateResult,
)
from .precheck import Precheck
from .tools.patch import PatchCheck


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


def from_phases(
    payload: dict[str, Any],
    pre: Precheck,
    v: ValidateResult,
    report: Report | None,
    *,
    assessment: Assessment | None,
    regression: RegressionCheck | None,
    analysis: Analysis | None,
    fix: FixProposal | None,
    fix_applies: dict[str, PatchCheck],
) -> AgentResult:
    """The report, with the fix and how it applies; or, if write-report failed, what the phases found."""
    base = {
        "bug": pre.bug,
        "package": pre.package,
        "short_title": _short_title(payload, v),
        "proposed_fix": fix.proposed_fix if fix else ProposedFix(kind=ProposedFixKind.none),
        "fix_applies": {ref: describe(c) for ref, c in fix_applies.items()},
    }
    fix_references = fix.references if fix else []
    if report is not None:
        references = list(dict.fromkeys([*report.references, *fix_references]))
        return AgentResult.model_validate(base | report.model_dump() | {"references": references})
    parts = [f"**Verdict: {v.verdict.value}.** {v.reason}"]
    if assessment is not None:
        parts.append(assessment.summary)
    if regression is not None:
        parts.append(regression.summary)
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
            "tags": ["server-triage-discuss"],
            "duplicate_of": assessment.duplicate_of if assessment else "",
            "analysis": "\n\n".join(p for p in parts if p),
            "references": fix_references,
            "sru_candidates": assessment.sru_candidates if assessment else [],
            "upstream_suggestion": upstream[0] if upstream else "",
        }
    )
