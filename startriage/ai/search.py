"""Host search: existing Launchpad and Debian reports matching the bug's facts.

Every search phrase is run against the bug's package, the package at fault
and the packages its notes list (``also_search``). Hits are ranked by how
many phrases found them. Debian has no full-text search: the subjects of the
package's bugs are matched instead.

The changelogs of the Ubuntu series then tell which Launchpad bugs an upload
already closed, and in which series a Debian fix has landed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from debian.debian_support import Version

from .phases import BugFacts, Candidate, ChangelogHit
from .tools import ToolError
from .tools.debian import matches, package_bugs
from .tools.http import ReadOnlyHTTP
from .tools.launchpad import in_lp_thread, search_tasks
from .tools.source import lp_bugs_closed, parse_changelog, read_file

logger = logging.getLogger(__name__)

PER_QUERY = 10
MAX_CANDIDATES = 20
MAX_DEBIAN = 10
MAX_ENTRY = 400
# Pockets users install from; Proposed is not released.
RELEASED_POCKETS = ("Release", "Security", "Updates")


def _search(lp: Any, packages: list[str], terms: list[str], own_bug: str) -> tuple[list[Candidate], int]:
    found: dict[str, Candidate] = {}
    queries = 0
    for package in packages:
        for term in terms:
            queries += 1
            try:
                hits = search_tasks(lp, package, term, include_closed=True, limit=PER_QUERY)
            except ToolError as exc:
                logger.info("Search skipped: %s", exc)
                break
            for hit in hits:
                if hit["bug"] == own_bug:
                    continue
                if hit["bug"] in found:
                    found[hit["bug"]].hits += 1
                    continue
                found[hit["bug"]] = Candidate(
                    source="launchpad",
                    ref=hit["bug"],
                    url=f"https://bugs.launchpad.net/bugs/{hit['bug']}",
                    title=hit["title"],
                    status=hit["status"],
                    package=package,
                )
    ranked = sorted(found.values(), key=lambda c: -c.hits)
    return ranked[:MAX_CANDIDATES], queries


async def search_launchpad(
    lp: Any, facts: BugFacts, own_bug: str, also: list[str]
) -> tuple[list[Candidate], int]:
    """Return ``(candidates, queries run)``; no Launchpad means no candidates."""
    if lp is None:
        return [], 0
    packages = [p for p in dict.fromkeys([facts.package, facts.target_package, *also]) if p]
    terms = [t.strip() for t in facts.search_terms if t.strip()]
    return await in_lp_thread(_search, lp, packages, terms, own_bug)


async def search_debian(http: ReadOnlyHTTP | None, packages: list[str], terms: list[str]) -> list[Candidate]:
    """Debian bugs of ``packages`` whose subject matches a search phrase, most hits first."""
    if http is None:
        return []
    found: list[Candidate] = []
    for package in packages:
        try:
            bugs = await package_bugs(http, package)
        except ToolError as exc:
            logger.info("Debian search of %s skipped: %s", package, exc)
            continue
        for bug in bugs:
            hits = sum(matches(bug.get("subject", ""), term) for term in terms)
            if hits:
                fixed = sorted(bug.get("fixed_in", []), key=_version_key)
                found.append(
                    Candidate(
                        source="debian",
                        ref=bug["bug"],
                        url=bug["url"],
                        title=bug.get("subject", ""),
                        status=" ".join(filter(None, [bug["status"], bug.get("severity")])),
                        package=package,
                        hits=hits,
                        fixed_version=fixed[-1] if fixed else "",
                    )
                )
    # Stable sort: bugs come newest first, so ties keep the newest.
    return sorted(found, key=lambda c: -c.hits)[:MAX_DEBIAN]


def _version_key(value: str) -> Any:
    try:
        return Version(value)
    except ValueError:
        return Version("0")


def current_versions(series: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The newest released version of the package in each series that has it."""
    current = {}
    for name, row in series.items():
        versions = [v for pocket, v in row.get("published", {}).items() if pocket in RELEASED_POCKETS]
        if versions:
            current[name] = max(versions, key=_version_key)
    return current


def _item(text: str, number: str) -> str:
    """The changelog bullet that mentions ``#number``."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(rf"#{number}\b", line):
            start = index
            while start > 0 and not re.match(r"^\s+[*-] ", lines[start]):
                start -= 1
            item = " ".join(part.strip() for part in lines[start : index + 1])
            return item[:MAX_ENTRY]
    return ""


async def _closed_in(
    http: ReadOnlyHTTP, package: str, series: str, version: str, numbers: set[str]
) -> list[ChangelogHit]:
    try:
        blocks = parse_changelog(await read_file(http, package, "debian/changelog", version))
    except ToolError as exc:
        logger.info("Changelog of %s %s not read: %s", package, version, exc)
        return []
    hits: dict[str, ChangelogHit] = {}
    # Oldest first: keep the first upload that closed each bug.
    for block in reversed(blocks):
        for number in sorted(lp_bugs_closed(block) & numbers):
            if number not in hits:
                hits[number] = ChangelogHit(
                    bug=number, series=series, version=str(block.version), entry=_item(str(block), number)
                )
    return list(hits.values())


async def changelog_hits(
    http: ReadOnlyHTTP | None, package: str, numbers: list[str], series: dict[str, dict[str, Any]]
) -> list[ChangelogHit]:
    """Uploads that closed one of the Launchpad bugs ``numbers``, per series."""
    if http is None or not numbers:
        return []
    wanted = set(numbers)
    current = current_versions(series)
    results = await asyncio.gather(
        *(_closed_in(http, package, name, version, wanted) for name, version in current.items())
    )
    return [hit for hits in results for hit in hits]


def mark_versions(
    candidates: list[Candidate], series: dict[str, dict[str, Any]], hits: list[ChangelogHit]
) -> None:
    """Fill ``in_series``: the series whose current version has the candidate's fix."""
    current = current_versions(series)
    for candidate in candidates:
        if candidate.source == "launchpad":
            candidate.in_series = sorted({h.series for h in hits if h.bug == candidate.ref})
        elif candidate.fixed_version:
            fixed = _version_key(candidate.fixed_version)
            candidate.in_series = sorted(name for name, v in current.items() if _version_key(v) >= fixed)


async def previous_version(http: ReadOnlyHTTP, package: str, version: str) -> str:
    """The version before ``version`` in its own changelog (``""`` if unknown)."""
    try:
        blocks = parse_changelog(await read_file(http, package, "debian/changelog", version))
    except ToolError as exc:
        logger.info("Changelog of %s %s not read: %s", package, version, exc)
        return ""
    return str(blocks[1].version) if len(blocks) > 1 else ""
