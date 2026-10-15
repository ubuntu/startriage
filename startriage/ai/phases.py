"""Hand-offs between triage phases.

Each model phase ends by calling a ``submit_<phase>`` tool whose parameters
are one of the models below; the host validates the call (and runs its own
checks) before the next phase starts. Host phases (precheck, search) build
their hand-offs directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contract import Importance, ProposedFix, ProposedFixKind, SRUCandidate, TriageStatus
from .tools.source import UpstreamInfo


class _Handoff(BaseModel):
    # LLM output is noisy; ignore harmless extra keys, validate the rest strictly.
    model_config = ConfigDict(extra="ignore")


class UpdateAction(StrEnum):
    none = "none"  # nothing to do
    comment = "comment"  # reply to the new activity
    change = "change"  # update status and/or tags
    escalate = "escalate"  # new information: triage the bug again


class UpdateReview(_Handoff):
    """Result of reviewing new activity on a bug the team already handled."""

    action: UpdateAction
    reason: str = Field(description="One or two sentences explaining the action")
    comment: str = Field("", description="Reply to post on the bug (action comment)")
    status: TriageStatus | None = Field(None, description="New status (action change)")
    tags: list[str] = Field([], description="Tags to add (action change)")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.action is UpdateAction.comment and not self.comment.strip():
            raise ValueError("comment is required when action is 'comment'")
        if self.action is UpdateAction.change and self.status is None and not self.tags:
            raise ValueError("status or tags is required when action is 'change'")
        return self


class Verdict(StrEnum):
    bug = "bug"  # a defect in Ubuntu
    feature = "feature"  # a request for new behaviour
    support = "support"  # a question or request for help
    config = "config"  # local misconfiguration or unsupported setup
    process = "process"  # merge, sync, MIR, FFe, SRU or similar tracking ticket
    test_infra = "test-infra"  # autopkgtest/CI infrastructure, not the package
    incomplete = "incomplete"  # cannot be judged without more information
    invalid = "invalid"  # not a bug, or not in Ubuntu
    team_owned = "team-owned"  # the team's own work item


# Verdicts that end the pipeline, and the status each one recommends.
EARLY_EXIT: dict[Verdict, TriageStatus] = {
    Verdict.support: TriageStatus.invalid,
    Verdict.config: TriageStatus.invalid,
    Verdict.invalid: TriageStatus.invalid,
    Verdict.incomplete: TriageStatus.incomplete,
    Verdict.process: TriageStatus.no_change,
    Verdict.test_infra: TriageStatus.no_change,
    Verdict.team_owned: TriageStatus.no_change,
}
# Early exits that need a comment for the reporter.
NEEDS_COMMENT = frozenset({Verdict.support, Verdict.config, Verdict.invalid, Verdict.incomplete})


class BugFacts(_Handoff):
    """The facts of a valid report, extracted once for the later phases."""

    package: str = Field(description="Ubuntu source package the bug is filed against")
    target_package: str = Field(
        "", description="Source package actually at fault, if not the filed one (else empty)"
    )
    affected_series: list[str] = Field([], description="Ubuntu series reported affected, e.g. noble")
    affected_versions: list[str] = Field([], description="Package versions reported affected")
    symptoms: str = Field(description="What goes wrong, in one paragraph")
    errors: list[str] = Field([], description="Error messages, copied verbatim")
    repro_steps: str = Field("", description="Steps to reproduce, if given")
    impact: str = Field("", description="Who is affected and how badly")
    search_terms: list[str] = Field(
        min_length=1,
        max_length=5,
        description="1-5 short Launchpad search phrases (2-4 distinctive words each)",
    )
    since_update: bool = Field(False, description="True if it started after a package update")


class ValidateResult(_Handoff):
    """Result of reading a new report: is it a valid bug, and its facts."""

    verdict: Verdict
    reason: str = Field(description="One or two sentences explaining the verdict")
    short_title: str = Field(description="Short title of the report (under 80 characters)")
    importance: Importance = Importance.undecided
    comment: str = Field(
        "", description="Comment for the reporter (required for incomplete, invalid, support, config)"
    )
    facts: BugFacts | None = Field(None, description="Required when verdict is bug or feature")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.verdict in (Verdict.bug, Verdict.feature) and self.facts is None:
            raise ValueError("facts is required when verdict is 'bug' or 'feature'")
        if self.verdict in NEEDS_COMMENT and not self.comment.strip():
            raise ValueError(f"comment is required when verdict is '{self.verdict.value}'")
        return self


class Candidate(BaseModel):
    """An existing report or fix found by the host search."""

    source: str
    ref: str
    url: str
    title: str
    status: str
    package: str
    hits: int = 1  # how many search phrases found it
    fixed_version: str = ""  # Debian: the newest version it is fixed in
    in_series: list[str] = []  # Ubuntu series whose current version has the fix


class ChangelogHit(BaseModel):
    """A Launchpad bug closed by an upload, as the series' changelog says."""

    bug: str
    series: str
    version: str  # the first upload in the series that closed it
    entry: str


class Upload(BaseModel):
    """A version published in an Ubuntu series shortly before the bug was filed."""

    series: str
    version: str
    pockets: list[str]
    published: str
    previous: str = ""  # the version before it in its changelog


class PackageContext(BaseModel):
    """What the host found about the package at fault, for the later phases."""

    package: str
    native: bool | None = None  # no Debian revision: Ubuntu or Debian is upstream
    upstream: UpstreamInfo | None = None
    changelog_hits: list[ChangelogHit] = []
    recent_uploads: list[Upload] = []


class KnownIssue(_Handoff):
    url: str
    summary: str = Field(description="How it relates to this bug")


class SeriesFix(_Handoff):
    series: str
    fixed: bool
    evidence: str = Field(description="Version, upload or bug proving it (or why not fixed)")


class FixCandidate(_Handoff):
    url: str
    merged: bool = Field(description="True if merged/released upstream, False if only proposed")
    summary: str


class Assessment(_Handoff):
    """What existing reports and fixes say about this bug."""

    duplicate_of: str = Field("", description="Launchpad bug number this duplicates, else empty")
    known_issues: list[KnownIssue] = []
    series: list[SeriesFix] = Field([], description="Fix state per affected Ubuntu series")
    sru_candidates: list[SRUCandidate] = []
    fix_candidates: list[FixCandidate] = []
    summary: str = Field(description="One paragraph: what is already known or fixed")


class Finding(_Handoff):
    """A report, discussion or fix found outside Launchpad and Debian."""

    source: str = Field(description="Where: upstream, fedora, arch, gentoo, ...")
    url: str = Field(description="The issue, pull/merge request or commit (as returned by a tool)")
    title: str
    status: str = Field("", description="As the tracker says: open, closed, merged, ...")
    summary: str = Field(description="How it relates to this bug")
    claims_fix: bool = Field(False, description="True if it is or points to a fix for this bug")
    merged: bool = Field(False, description="True if that fix is merged or released")
    fixed_version: str = Field("", description="Upstream release or commit with the fix, if known")
    has_reproducer: bool = False
    activity: str = Field("", description="Last activity, e.g. 'last comment 2026-09'")


class SearchResult(_Handoff):
    """Result of searching trackers outside Launchpad."""

    searched: list[str] = Field([], description="The trackers and queries you ran")
    findings: list[Finding] = []
    summary: str = Field(description="One or two sentences: what was found, or that nothing was")


class RegressionCheck(_Handoff):
    """Did a recent update cause the bug?"""

    confirmed: bool = Field(description="True only if a change in the update explains the bug")
    suspect_version: str = Field("", description="The update that broke it (from the suspects)")
    previous_version: str = Field("", description="The version before it (from the suspects)")
    hunk: str = Field("", description="The diff lines that cause it, copied verbatim with their +/- prefixes")
    summary: str = Field(description="What you compared and concluded")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.confirmed and not (self.suspect_version and self.previous_version and self.hunk.strip()):
            raise ValueError("suspect_version, previous_version and hunk are required when confirmed")
        return self


class Confidence(StrEnum):
    high = "high"  # the faulty code was read and explains every symptom
    medium = "medium"  # the code explains the symptom; a detail is unchecked
    low = "low"  # a plausible area of the code
    none = "none"  # the code involved was not found


class CodeLocation(_Handoff):
    path: str = Field(description="Path in the source package, e.g. src/http/ngx_http_parse.c")
    line: int = Field(0, description="Line number at ref (0 if not one line)")
    ref: str = Field("", description="Series or version you read it at (default: devel)")
    note: str = Field(description="What is wrong there")


class Analysis(_Handoff):
    """Result of analysing the source: where and why the bug happens."""

    root_cause: str = Field(description="The mechanism of the bug, or what is still unknown")
    confidence: Confidence
    locations: list[CodeLocation] = Field([], description="The code you read that causes the bug")
    summary: str = Field(description="What you checked")
    upstream_suggestion: str = Field("", description="What to report upstream, if anything")

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.confidence is Confidence.high and not self.locations:
            raise ValueError("locations are required when confidence is 'high'")
        return self


class FixProposal(_Handoff):
    """Result of proposing a fix: an existing one, a diff, or none."""

    proposed_fix: ProposedFix = ProposedFix(kind=ProposedFixKind.none)
    unmerged: bool = Field(False, description="True if the referenced fix is not merged upstream yet")
    summary: str = Field(description="What you checked, and how the fix applies")
    references: list[str] = Field([], description="URLs you verified")
    upstream_suggestion: str = Field("", description="What to report or propose upstream, if anything")


class Report(_Handoff):
    """The final recommendation for the triager."""

    status: TriageStatus
    importance: Importance = Importance.undecided
    series: dict[str, TriageStatus] = Field({}, description="Recommended status per Ubuntu series")
    tags: list[str] = Field([], description="Tags to add")
    duplicate_of: str = Field("", description="Launchpad bug number (status Duplicate)")
    analysis: str = Field(description="Markdown summary for the triager")
    suggested_comment: str = Field("", description="Comment to post on the bug")
    references: list[str] = []
    sru_candidates: list[SRUCandidate] = []
    upstream_suggestion: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (
            self.status in (TriageStatus.incomplete, TriageStatus.invalid)
            and not self.suggested_comment.strip()
        ):
            raise ValueError(f"suggested_comment is required when status is '{self.status.value}'")
        if self.status is TriageStatus.duplicate and not self.duplicate_of.strip():
            raise ValueError("duplicate_of is required when status is 'Duplicate'")
        return self


class PhaseState(StrEnum):
    done = "done"
    skipped = "skipped"
    failed = "failed"


@dataclass
class PhaseOutcome:
    """What one phase did for one bug, for the report and the logs."""

    phase: str
    state: PhaseState
    summary: str = ""
    result: BaseModel | None = None
    tool_calls: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    sessions: int = 0
    feedback: str = ""
    skill_sha256: str = ""
