"""Host checks run on every ``submit_*`` call.

A failed check is returned to the model in the same session, like a schema
error, so it can fix the claim. The checks only reject what the host can
verify: series names, bug numbers, URLs, tags it cannot vouch for yet, code
locations, regression hunks and proposed diffs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .contract import ProposedFix, ProposedFixKind
from .phases import (
    Analysis,
    Assessment,
    FixProposal,
    RegressionCheck,
    Report,
    SearchResult,
    UpdateReview,
    Upload,
)
from .tools import ToolError
from .tools.http import NetworkError, ReadOnlyHTTP
from .tools.patch import (
    ApplyState,
    FilePatch,
    PatchCheck,
    applied_ref,
    check_patch,
    guess_strip,
    parse_patch,
    read_patch,
)
from .tools.source import checked_ref, read_diff, read_file
from .tools.web import url_status

logger = logging.getLogger(__name__)

MAX_LOCATIONS = 5
_APPLIES = frozenset({ApplyState.clean, ApplyState.offset})

# Tags the host must verify before suggesting them.
UNVERIFIABLE_TAGS = {
    "regression-update": "needs a regression in an update confirmed by the check-regression phase",
    "bitesize": "needs a proposed fix that the host verified to apply without fuzz to every series checked",
}
_URL = re.compile(r"^https?://\S+$")


def check_tags(tags: Iterable[str], verified: Iterable[str] = ()) -> list[str]:
    """Reject the tags the host has to verify, unless it did (``verified``)."""
    allowed = set(verified)
    return [
        f"remove tag {tag!r}: it {why}"
        for tag in tags
        if tag not in allowed and (why := UNVERIFIABLE_TAGS.get(tag))
    ]


def _changed_lines(text: str) -> list[str]:
    return [
        line.rstrip()
        for line in text.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and line[1:].strip()
    ]


def check_hunk(hunk: str, diff: str) -> list[str]:
    """The changed lines quoted in ``hunk`` must all be in ``diff``."""
    quoted = _changed_lines(hunk)
    if not quoted:
        return ["hunk must quote the changed lines of the diff, with their + or - prefix"]
    present = set(_changed_lines(diff))
    missing = [line for line in quoted if line not in present]
    if missing:
        return [
            f"hunk line {missing[0]!r} is not in the diff between the versions"
            + (f" ({len(missing) - 1} more missing)" if len(missing) > 1 else "")
            + "; copy the lines verbatim from source_diff"
        ]
    return []


def check_series(names: Iterable[str], series: dict[str, Any]) -> list[str]:
    """Series names must come from the series table (unchecked when there is none)."""
    if not series:
        return []
    known = ", ".join(series)
    return [
        f"series {name!r} is not an active Ubuntu series (use one of: {known})"
        for name in names
        if name not in series
    ]


def check_bug_ref(value: str, own_bug: str, field: str = "duplicate_of") -> list[str]:
    value = value.strip().lstrip("#")
    if not value:
        return []
    if not value.isdigit():
        return [f"{field} must be a Launchpad bug number, got {value!r}"]
    if value == own_bug:
        return [f"{field} cannot be the bug itself"]
    return []


def urls_in(values: Iterable[str]) -> list[str]:
    return [value.strip() for value in values if _URL.match(value.strip())]


class URLChecker:
    """Checks that cited URLs resolve, once per URL per run."""

    def __init__(self, http: ReadOnlyHTTP | None) -> None:
        self.http = http
        self._seen: dict[str, str | None] = {}

    async def _problem(self, url: str) -> str | None:
        assert self.http is not None
        try:
            status, _ = await url_status(self.http, url)
        except NetworkError as exc:
            # A network failure is not the model's fault: accept the URL.
            logger.warning("Could not verify %s: %s", url, exc)
            return None
        except ToolError as exc:
            return f"URL {url} cannot be cited: {exc}"
        except Exception as exc:
            logger.warning("Could not verify %s: %s", url, exc)
            return None
        if status >= 400:
            return f"URL {url} does not resolve (HTTP {status}); remove it or cite one that exists"
        return None

    async def check(self, urls: Iterable[str]) -> list[str]:
        if self.http is None:
            return []
        problems = []
        for url in dict.fromkeys(urls):
            if url not in self._seen:
                self._seen[url] = await self._problem(url)
            if problem := self._seen[url]:
                problems.append(problem)
        return problems


def describe(check: PatchCheck) -> str:
    failing = [f"{f.path}: {f.detail}" for f in check.files if f.state is ApplyState.fails]
    text = check.state.value + (f" ({'; '.join(failing)})" if failing else "")
    return text if check.patches_applied else text + " [without the package's patches applied]"


def bitesize(found: dict[str, PatchCheck]) -> bool:
    """A fix that applies without fuzz everywhere it is not already applied.

    Only trees with the package's own patches applied count: a clean apply to
    the plain tree may still conflict with them.
    """
    states = {c.state for c in found.values()}
    return (
        bool(states & _APPLIES)
        and states <= _APPLIES | {ApplyState.applied}
        and all(c.patches_applied for c in found.values())
    )


@dataclass
class HostChecks:
    """The checks of each phase's submission (those reading the network pass without ``http``)."""

    http: ReadOnlyHTTP | None
    urls: URLChecker

    async def review(self, r: UpdateReview) -> list[str]:
        return check_tags(r.tags)

    async def assessment(self, a: Assessment, bug: str, series: dict[str, Any]) -> list[str]:
        problems = check_series([s.series for s in a.series], series)
        problems += check_series([c.series for c in a.sru_candidates], series)
        problems += check_bug_ref(a.duplicate_of, bug)
        urls = [k.url for k in a.known_issues] + [f.url for f in a.fix_candidates]
        return problems + await self.urls.check(urls)

    async def search(self, s: SearchResult) -> list[str]:
        return await self.urls.check(f.url for f in s.findings)

    async def regression(self, r: RegressionCheck, package: str, suspects: list[Upload]) -> list[str]:
        if not r.confirmed:
            return []
        suspect = next((u for u in suspects if u.version == r.suspect_version), None)
        if suspect is None:
            return [f"suspect_version must be one of: {', '.join(u.version for u in suspects)}"]
        if r.previous_version != suspect.previous:
            return [f"previous_version of {suspect.version} is {suspect.previous}"]
        if self.http is None:
            return []
        try:
            diff = await read_diff(self.http, package, suspect.previous, suspect.version)
        except NetworkError as exc:
            logger.warning("Could not verify the regression hunk: %s", exc)
            return []
        except ToolError as exc:
            return [f"the host cannot read the diff to verify the hunk ({exc}); do not confirm"]
        return check_hunk(r.hunk, diff)

    async def analysis(self, a: Analysis, package: str) -> list[str]:
        """Each location must exist at its ref (patches applied), with the line in the file."""
        if self.http is None:
            return []
        problems = []
        for loc in a.locations[:MAX_LOCATIONS]:
            try:
                ref, _ = await checked_ref(self.http, package, applied_ref(loc.ref))
                text = await read_file(self.http, package, loc.path.strip().lstrip("/"), ref)
            except NetworkError as exc:
                logger.warning("Could not verify the analysis locations: %s", exc)
                return problems
            except ToolError as exc:
                if "not imported" in str(exc):
                    return []
                problems.append(f"location {loc.path}: {exc}")
                continue
            count = len(text.splitlines())
            if loc.line > count:
                problems.append(f"location {loc.path}:{loc.line}: the file has only {count} lines at {ref}")
        return problems

    async def fix(self, f: FixProposal, package: str, refs: list[str]) -> list[str]:
        fix = f.proposed_fix
        cited = [fix.value] if fix.kind is ProposedFixKind.reference else []
        problems = await self.urls.check(urls_in([*f.references, *cited]))
        if fix.kind is not ProposedFixKind.diff:
            return problems
        try:
            files = parse_patch(fix.value)
        except ToolError as exc:
            return [*problems, f"proposed_fix is not a usable unified diff: {exc}"]
        found = await self._patch_checks(package, refs, files)
        # A diff that is already applied somewhere is real too; one that applies nowhere is not.
        if found and all(c.state is ApplyState.fails for c in found.values()):
            details = "\n".join(c.describe() for c in found.values())
            problems.append(
                f"the diff applies to none of the checked series; check it with patch_applies:\n{details}"
            )
        return problems

    async def report(self, r: Report, bug: str, series: dict[str, Any], verified: set[str]) -> list[str]:
        problems = check_series(r.series, series)
        problems += check_series([c.series for c in r.sru_candidates], series)
        problems += check_tags(r.tags, verified)
        problems += check_bug_ref(r.duplicate_of, bug)
        return problems + await self.urls.check(urls_in(r.references))

    async def fix_applies(self, package: str, refs: list[str], fix: ProposedFix) -> dict[str, PatchCheck]:
        """How the proposed fix applies per series, if it is a diff or a patch URL."""
        if self.http is None or fix.kind is ProposedFixKind.none:
            return {}
        try:
            text = (
                fix.value
                if fix.kind is ProposedFixKind.diff
                else await read_patch(self.http, fix.value.strip())
            )
            files = parse_patch(text)
        except (ToolError, NetworkError) as exc:
            logger.info("Cannot check whether the proposed fix applies: %s", exc)
            return {}
        return await self._patch_checks(package, refs, files)

    async def _patch_checks(
        self, package: str, refs: list[str], files: list[FilePatch]
    ) -> dict[str, PatchCheck]:
        """How a patch applies at each of ``refs`` (those the host could read)."""
        found: dict[str, PatchCheck] = {}
        if self.http is None:
            return found
        strip = guess_strip(files)
        for ref in refs:
            try:
                found[ref] = await check_patch(self.http, files, package, ref, strip)
            except NetworkError as exc:
                logger.warning("Could not check the fix against %s: %s", ref, exc)
                break
            except ToolError as exc:
                logger.info("Could not check the fix against %s: %s", ref, exc)
        return found
