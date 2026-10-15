"""Host-orchestrated triage pipeline: precheck, then one session per phase.

::

    precheck (host) ─ denied / skip ──────────────────────────────▶ no session
                    ─ update ─▶ review-update ─ (escalate) ─┐
                    ─ new ─────────────────────────────────┴▶ validate-report
    validate-report ─ early exit (support, invalid, ...) ─────────▶ host result
                    ─ bug / feature ─▶ search (host: Launchpad, Debian, changelogs)
      ─▶ search-upstream ─▶ search-other-distros (no upstream fix)   [not native,
                                                                      not closed]
      ─▶ assess-existing (anything found)
      ─▶ check-regression (recent update)
      ─▶ analyze-source (no fix known) ─▶ propose-fix (a fix or a root cause)   [bugs, unresolved]
      ─▶ write-report

The host decides what runs next; each phase only judges.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import archive, results
from .agent import BugOutcome, run_phase
from .checks import HostChecks, URLChecker, bitesize, describe
from .contract import ProposedFixKind
from .feedback import record_feedback
from .phases import (
    EARLY_EXIT,
    Analysis,
    Assessment,
    BugFacts,
    Candidate,
    Confidence,
    Finding,
    FixProposal,
    PackageContext,
    PhaseOutcome,
    PhaseState,
    RegressionCheck,
    Report,
    SearchResult,
    UpdateAction,
    UpdateReview,
    ValidateResult,
    Verdict,
)
from .precheck import Precheck, Route, Window, build_precheck
from .provider import Provider
from .search import changelog_hits, current_versions, mark_versions, search_debian, search_launchpad
from .skills import PIPELINE, also_search, load_skill, system_prompt
from .tools import Submission, ToolSpec
from .tools.http import ReadOnlyHTTP
from .tools.launchpad import model_payload
from .tools.patch import PatchCheck

logger = logging.getLogger(__name__)

DENIED_NOTE = "Private bug: not sent to the AI."
SKIP_NOTE = "Only team activity in the triage window and no new comment: nothing to triage."
# Affected series a proposed fix is checked against (devel is always checked too).
MAX_FIX_SERIES = 3


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_defaults=True)
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value


def _precheck_facts(pre: Precheck, *, update: bool) -> dict[str, Any]:
    """The precheck as given to a phase (the update review also gets the activity)."""
    exclude = {"route", "series"}
    if not update:
        exclude |= {"window_events", "last_team_comment", "new_comments"}
    return pre.model_dump(mode="json", exclude=exclude, exclude_defaults=True)


@dataclass
class _Bug:
    """A bug past validate-report, and what the phases found so far."""

    payload: dict[str, Any]
    outcome: BugOutcome
    pre: Precheck
    v: ValidateResult
    # The package at fault: its upstream, changelogs and updates matter.
    package: str
    # The packages whose notes the phases get.
    packages: list[str]
    series: dict[str, dict[str, Any]]
    context: PackageContext
    candidates: list[Candidate] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    assessment: Assessment | None = None
    regression: archive.Regression = field(default_factory=lambda: archive.Regression([]))
    analysis: Analysis | None = None
    fix: FixProposal | None = None
    fix_applies: dict[str, PatchCheck] = field(default_factory=dict)

    @property
    def number(self) -> str:
        return self.outcome.bug

    @property
    def facts(self) -> BugFacts:
        assert self.v.facts is not None
        return self.v.facts

    @property
    def is_bug(self) -> bool:
        return self.v.verdict is Verdict.bug

    @property
    def affected(self) -> list[str]:
        return list(dict.fromkeys(self.facts.affected_series or self.pre.affected_series))

    @property
    def settled(self) -> str:
        """Why the bug needs no regression check, analysis or fix (``""`` if it does)."""
        if not self.is_bug:
            return "feature request"
        a = self.assessment
        if a is None:
            return ""
        # A duplicate, or fixed in every affected series.
        relevant = [s for s in a.series if not self.affected or s.series in self.affected]
        if a.duplicate_of or (relevant and all(s.fixed for s in relevant)):
            return "settled by existing reports"
        return ""

    @property
    def known_fix(self) -> bool:
        return bool(
            (self.assessment is not None and self.assessment.fix_candidates)
            or any(f.claims_fix for f in self.findings)
            or self.regression.confirmed
        )

    @property
    def refs(self) -> list[str]:
        """The series a proposed fix is checked against: the affected ones, and devel."""
        return [*[s for s in self.affected if s in self.series][:MAX_FIX_SERIES], "devel"]


@dataclass
class Pipeline:
    """Everything one triage run needs, shared by all bugs."""

    provider: Provider
    tools: dict[str, ToolSpec] = field(default_factory=dict)
    lp: Any = None
    http: ReadOnlyHTTP | None = None
    team: frozenset[str] = frozenset()
    window: Window | None = None
    urls: URLChecker = field(default_factory=lambda: URLChecker(None))
    feedback: bool = False
    feedback_log: Path | None = None
    model: str = ""
    working_directory: Path | None = None
    # Called with the phase name when a phase starts (for a progress line).
    on_phase: Callable[[str], None] | None = None
    checks: HostChecks = field(init=False)

    def __post_init__(self) -> None:
        self.checks = HostChecks(self.http, self.urls)

    async def _phase[M: BaseModel](
        self,
        outcome: BugOutcome,
        packages: list[str],
        phase: str,
        model: type[M],
        data: dict[str, Any],
        *,
        check: Callable[[M], Any] | None = None,
        summary: Callable[[M], str] | None = None,
    ) -> M | None:
        """Run one phase session and record it; return what it submitted (``None`` if it failed)."""
        skill = load_skill(phase)
        if self.on_phase is not None:
            self.on_phase(phase)
        submission = Submission(
            skill.submit,
            model,
            description=f"Submit the result of this phase ({skill.description}) Call it once, when done.",
            check=check,
            feedback=self.feedback,
        )
        task = f"Launchpad bug {outcome.bug}, phase {phase}. Input:\n" + json.dumps(
            {key: _dump(value) for key, value in data.items()}, ensure_ascii=False, indent=1
        )
        done = await run_phase(
            self.provider,
            phase=phase,
            bug=outcome.bug,
            system_prompt=system_prompt(phase, packages),
            task=task,
            tools=[self.tools[name] for name in skill.tools if name in self.tools],
            submission=submission,
            skill_sha256=skill.sha256,
            working_directory=self.working_directory,
            max_calls=skill.max_calls,
        )
        outcome.phases.append(done)
        if not isinstance(done.result, model):
            return None
        if summary is not None:
            done.summary = summary(done.result)
        return done.result

    def _needs(self, phase: str) -> str:
        """Why ``phase`` cannot run with the enabled tools (``""`` if it can)."""
        missing = load_skill(phase).missing(self.tools)
        return "needs " + ", ".join(m.replace("|", " or ") for m in missing) if missing else ""

    def _skipped(self, outcome: BugOutcome, phase: str, *reasons: str) -> bool:
        """Record ``phase`` as skipped for the first of ``reasons``, or for a disabled tool."""
        why = next((r for r in reasons if r), "") or self._needs(phase)
        if why:
            outcome.phases.append(PhaseOutcome(phase, PhaseState.skipped, summary=why))
        return bool(why)

    @staticmethod
    def _failed(outcome: BugOutcome) -> None:
        phase = outcome.phases[-1]
        outcome.error = f"{phase.phase} failed: {phase.summary}"

    # -- the pipeline ----------------------------------------------------------------------

    async def triage(self, payload: dict[str, Any]) -> BugOutcome:
        outcome = BugOutcome(bug=str(payload.get("number", "")))
        pre = await self._precheck(payload, outcome)
        if pre is None:
            return outcome
        if pre.route is Route.update and not await self._review_update(payload, pre, outcome):
            return outcome
        v = await self._validate(payload, pre, outcome)
        if v is None:
            return outcome
        bug = await self._search(payload, pre, v, outcome)
        await self._search_outside(bug)
        await self._assess(bug)
        await self._check_regression(bug)
        await self._analyze(bug)
        await self._propose_fix(bug)
        await self._write_report(bug)
        return outcome

    async def _precheck(self, payload: dict[str, Any], outcome: BugOutcome) -> Precheck | None:
        """The host's look at the bug; ``None`` if it goes no further."""
        package = payload.get("package") or ""
        series = {} if payload.get("private") else await archive.series(self.lp, package)
        pre = build_precheck(payload, team=self.team, window=self.window, series=series)
        outcome.route = pre.route.value
        reasons = "; ".join(pre.reasons)
        outcome.phases.append(
            PhaseOutcome("precheck", PhaseState.done, summary=f"route {pre.route.value}: {reasons}")
        )
        logger.info("Bug %s route=%s (%s)", outcome.bug, pre.route.value, reasons)
        if pre.route is Route.denied:
            outcome.note = DENIED_NOTE
            return None
        if pre.route is Route.skip:
            outcome.note = SKIP_NOTE
            return None
        return pre

    async def _review_update(self, payload: dict[str, Any], pre: Precheck, outcome: BugOutcome) -> bool:
        """Review the activity on a triaged bug; True if it needs a full triage."""
        review = await self._phase(
            outcome,
            [pre.package],
            "review-update",
            UpdateReview,
            {"precheck": _precheck_facts(pre, update=True), "series": pre.series},
            check=self.checks.review,
            summary=lambda r: f"{r.action.value}: {r.reason}",
        )
        if review is None:
            self._failed(outcome)
            return False
        if review.action is UpdateAction.escalate:
            return True
        outcome.result = results.from_review(payload, pre, review)
        return False

    async def _validate(
        self, payload: dict[str, Any], pre: Precheck, outcome: BugOutcome
    ) -> ValidateResult | None:
        """The verdict; ``None`` if the bug goes no further (an early exit, or a failure)."""
        v = await self._phase(
            outcome,
            [pre.package],
            "validate-report",
            ValidateResult,
            {
                "bug": model_payload(payload),
                "precheck": _precheck_facts(pre, update=False),
                "series": pre.series,
            },
            summary=lambda v: f"verdict {v.verdict.value}: {v.reason}",
        )
        if v is None:
            self._failed(outcome)
            return None
        hints = ", ".join(pre.process_hints) or "none"
        logger.info("Bug %s verdict=%s, process hints: %s", outcome.bug, v.verdict.value, hints)
        if v.verdict in EARLY_EXIT:
            outcome.result = results.from_verdict(payload, pre, v)
            return None
        return v

    async def _search(
        self, payload: dict[str, Any], pre: Precheck, v: ValidateResult, outcome: BugOutcome
    ) -> _Bug:
        """The host search: the package at fault, and the existing reports and fixes."""
        assert v.facts is not None
        facts = v.facts
        package = facts.target_package or facts.package or pre.package
        series = pre.series if package == pre.package else await archive.series(self.lp, package)
        packages = [p for p in dict.fromkeys([facts.package or pre.package, facts.target_package]) if p]
        if self.on_phase:
            self.on_phase("search")
        context = await archive.package_context(self.http, package, series)
        bug = _Bug(payload, outcome, pre, v, package, packages, series, context)
        launchpad, queries = await search_launchpad(self.lp, facts, bug.number, also_search(packages))
        terms = [t.strip() for t in facts.search_terms if t.strip()]
        debian = await search_debian(self.http, packages, terms)
        numbers = [bug.number, *(c.ref for c in launchpad)]
        context.changelog_hits = await changelog_hits(self.http, package, numbers, series)
        bug.candidates = launchpad + debian
        mark_versions(bug.candidates, series, context.changelog_hits)
        if bug.is_bug:
            context.recent_uploads = await archive.uploads_since(self.lp, package, archive.filed(payload))
        if self.lp is None and self.http is None:
            outcome.phases.append(
                PhaseOutcome(
                    "search", PhaseState.skipped, summary="Launchpad and the network are not available"
                )
            )
            return bug
        summary = (
            f"{len(launchpad)} Launchpad candidates from {queries} queries, "
            f"{len(debian)} Debian candidates, {len(context.changelog_hits)} changelog hits"
        )
        logger.info("Bug %s search: %s", bug.number, summary)
        outcome.phases.append(PhaseOutcome("search", PhaseState.done, summary=summary))
        return bug

    async def _search_outside(self, bug: _Bug) -> None:
        """Search upstream, then other distributions if upstream has no fix."""
        closed = [h for h in bug.context.changelog_hits if h.bug == bug.number]
        why_not = (
            "feature request"
            if not bug.is_bug
            else "native package: no separate upstream"
            if bug.context.native
            else f"already closed by {closed[0].version} ({closed[0].series})"
            if closed
            else ""
        )
        upstream = await self._tracker_search(
            bug,
            "search-upstream",
            why_not,
            {
                "facts": bug.facts,
                "upstream": bug.context.upstream,
                "current_versions": current_versions(bug.series),
            },
        )
        if not why_not and any(f.claims_fix for f in bug.findings):
            why_not = "upstream has a fix"
        await self._tracker_search(
            bug,
            "search-other-distros",
            why_not,
            {"facts": bug.facts, "upstream_search": upstream.summary if upstream else "not run"},
        )

    async def _tracker_search(
        self, bug: _Bug, phase: str, why_not: str, data: dict[str, Any]
    ) -> SearchResult | None:
        if self._skipped(bug.outcome, phase, why_not):
            return None
        found = await self._phase(
            bug.outcome,
            bug.packages,
            phase,
            SearchResult,
            data,
            check=self.checks.search,
            summary=lambda s: s.summary,
        )
        if found is not None:
            bug.findings += found.findings
        return found

    async def _assess(self, bug: _Bug) -> None:
        found = bug.candidates or bug.findings or bug.context.changelog_hits
        if self._skipped(bug.outcome, "assess-existing", "" if found else "nothing found"):
            return
        bug.assessment = await self._phase(
            bug.outcome,
            bug.packages,
            "assess-existing",
            Assessment,
            {
                "facts": bug.facts,
                "candidates": bug.candidates,
                "findings": bug.findings,
                "changelog_hits": bug.context.changelog_hits,
                "series": bug.pre.series,
            },
            check=lambda a: self.checks.assessment(a, bug.number, bug.pre.series),
            summary=lambda a: a.summary,
        )

    async def _check_regression(self, bug: _Bug) -> None:
        regression = bug.regression
        if not bug.settled:
            regression.suspects = await archive.suspects(
                self.http, bug.package, bug.facts, set(bug.affected), bug.context, bug.series
            )
        why_not = bug.settled or ("" if regression.suspects else "no recent update to suspect")
        if self._skipped(bug.outcome, "check-regression", why_not):
            return
        regression.result = await self._phase(
            bug.outcome,
            [bug.package],
            "check-regression",
            RegressionCheck,
            {"facts": bug.facts, "suspects": regression.suspects},
            check=lambda r: self.checks.regression(r, bug.package, regression.suspects),
            summary=lambda r: ("confirmed: " if r.confirmed else "not confirmed: ") + r.summary,
        )

    async def _analyze(self, bug: _Bug) -> None:
        known = "a fix is already known" if bug.known_fix else ""
        if self._skipped(bug.outcome, "analyze-source", bug.settled, known):
            return
        bug.analysis = await self._phase(
            bug.outcome,
            bug.packages,
            "analyze-source",
            Analysis,
            {
                "package": bug.package,
                "facts": bug.facts,
                "series": bug.pre.series,
                "assessment": bug.assessment,
                "findings": bug.findings,
                "regression": bug.regression.result,
            },
            check=lambda a: self.checks.analysis(a, bug.package),
            summary=lambda a: f"confidence {a.confidence.value}: {a.summary}",
        )

    async def _propose_fix(self, bug: _Bug) -> None:
        """Run propose-fix, then check how the fix applies to each series checked."""
        rooted = bug.analysis is not None and bug.analysis.confidence in (Confidence.high, Confidence.medium)
        unknown = "" if bug.known_fix or rooted else "no fix known and no root cause found"
        if self._skipped(bug.outcome, "propose-fix", bug.settled, unknown):
            return
        refs = bug.refs
        bug.fix = await self._phase(
            bug.outcome,
            bug.packages,
            "propose-fix",
            FixProposal,
            {
                "package": bug.package,
                "facts": bug.facts,
                "check_refs": refs,
                "fix_candidates": bug.assessment.fix_candidates if bug.assessment else [],
                "findings": [f for f in bug.findings if f.claims_fix],
                "regression": bug.regression.confirmed,
                "analysis": bug.analysis,
            },
            check=lambda f: self.checks.fix(f, bug.package, refs),
        )
        if bug.fix is None:
            return
        bug.fix_applies = await self.checks.fix_applies(bug.package, refs, bug.fix.proposed_fix)
        applies = ", ".join(f"{ref} {c.state.value}" for ref, c in bug.fix_applies.items())
        kind = bug.fix.proposed_fix.kind.value
        bug.outcome.phases[-1].summary = kind + (f" ({applies})" if applies else "") + f": {bug.fix.summary}"

    async def _write_report(self, bug: _Bug) -> None:
        verified = {"regression-update"} if bug.regression.update else set()
        fix = bug.fix
        if (
            fix is not None
            and fix.proposed_fix.kind is not ProposedFixKind.none
            and bitesize(bug.fix_applies)
        ):
            verified.add("bitesize")
        report = await self._phase(
            bug.outcome,
            bug.packages,
            "write-report",
            Report,
            {
                "precheck": _precheck_facts(bug.pre, update=False),
                "validation": bug.v,
                "candidates": bug.candidates,
                "findings": bug.findings,
                "changelog_hits": bug.context.changelog_hits,
                "assessment": bug.assessment,
                "regression": bug.regression.result,
                "regression_update": bug.regression.update,
                "analysis": bug.analysis,
                "fix": fix,
                "fix_applies": {ref: describe(c) for ref, c in bug.fix_applies.items()},
                "bitesize_verified": "bitesize" in verified,
                "series": bug.pre.series,
            },
            check=lambda r: self.checks.report(r, bug.number, bug.pre.series, verified),
            summary=lambda r: f"status {r.status.value}",
        )
        bug.outcome.result = results.from_phases(
            bug.payload,
            bug.pre,
            bug.v,
            report,
            assessment=bug.assessment,
            regression=bug.regression.result,
            analysis=bug.analysis,
            fix=fix,
            fix_applies=bug.fix_applies,
        )

    def _record_feedback(self, outcome: BugOutcome) -> None:
        if not (self.feedback and self.feedback_log):
            return
        entries = [
            {
                "skill": p.phase,
                "skill_sha256": p.skill_sha256,
                "bug": outcome.bug,
                "model": self.model,
                "feedback": p.feedback,
            }
            for p in outcome.phases
            if p.feedback
        ]
        if entries:
            try:
                record_feedback(entries, self.feedback_log)
            except OSError as exc:
                logger.warning("Could not record skill feedback: %s", exc)


def _log_outcome(outcome: BugOutcome) -> None:
    """Emit a per-bug step log: the decision at -v, deeper detail at -vv."""
    if outcome.result is not None:
        result = outcome.result
        logger.info(
            "Bug %s → status=%s, tags=%s",
            outcome.bug,
            result.status.value,
            ", ".join(result.tags) or "(none)",
        )
        logger.debug("Bug %s proposed fix: %s", outcome.bug, result.proposed_fix.kind.value)
        for phase in outcome.phases:
            logger.debug("Bug %s %s (%s): %s", outcome.bug, phase.phase, phase.state.value, phase.summary)
    elif outcome.error is not None:
        logger.warning("Bug %s failed: %s", outcome.bug, outcome.error)
    else:
        logger.info("Bug %s not sent to the AI: %s", outcome.bug, outcome.note)


def phase_totals(outcomes: list[BugOutcome]) -> dict[str, tuple[int, int, int]]:
    """Sessions, input and output tokens per phase over ``outcomes``."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for outcome in outcomes:
        for phase in outcome.phases:
            if phase.sessions:
                row = totals[phase.phase]
                row[0] += phase.sessions
                row[1] += phase.input_tokens
                row[2] += phase.output_tokens
    order = {name: index for index, name in enumerate(PIPELINE)}
    ranked = sorted(totals.items(), key=lambda item: order.get(item[0], len(order)))
    return {name: (s, i, o) for name, (s, i, o) in ranked}


async def triage_bugs(
    pipeline: Pipeline,
    payloads: list[dict],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[BugOutcome]:
    """Triage ``payloads`` sequentially, recording per-bug failures and continuing.

    ``on_progress`` (when given) is called as ``(index, total, bug)`` before each
    bug, so a caller can drive a spinner/progress line.
    """
    total = len(payloads)
    outcomes: list[BugOutcome] = []
    for index, payload in enumerate(payloads, start=1):
        bug = str(payload.get("number", ""))
        if on_progress is not None:
            on_progress(index, total, bug)
        logger.info("Triaging bug %s (%d/%d)…", bug, index, total)
        try:
            outcome = await pipeline.triage(payload)
        except Exception as exc:
            logger.warning("Bug %s: triage failed", bug, exc_info=True)
            outcome = BugOutcome(bug=bug, error=f"{type(exc).__name__}: {exc}")
        _log_outcome(outcome)
        pipeline._record_feedback(outcome)
        outcomes.append(outcome)
    succeeded = sum(o.ok for o in outcomes)
    failed = sum(o.error is not None for o in outcomes)
    logger.info(
        "AI triage complete: %d succeeded, %d failed, %d not sent",
        succeeded,
        failed,
        total - succeeded - failed,
    )
    for name, (sessions, tokens_in, tokens_out) in phase_totals(outcomes).items():
        logger.info(
            "Phase %s: %d sessions, %d input / %d output tokens", name, sessions, tokens_in, tokens_out
        )
    return outcomes
