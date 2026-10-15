"""What the host reads about the package at fault: versions, upstream, uploads.

The pipeline gives this to the phases as facts, and picks the updates to
suspect of a regression from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from debian.debian_support import Version

from .phases import BugFacts, PackageContext, RegressionCheck, Upload
from .search import current_versions, previous_version
from .tools import ToolError
from .tools.http import ReadOnlyHTTP
from .tools.launchpad import in_lp_thread, recent_uploads, series_table
from .tools.source import read_upstream_info

logger = logging.getLogger(__name__)

# Pockets whose uploads count as updates (a regression there is regression-update).
UPDATE_POCKETS = frozenset({"Updates", "Security"})
MAX_SUSPECTS = 2


async def series(lp: Any, package: str) -> dict[str, dict[str, Any]]:
    """The package's versions per active series (empty when unknown)."""
    if lp is None or not package:
        return {}
    try:
        return await in_lp_thread(series_table, lp, package)
    except Exception as exc:
        logger.warning("Could not read the versions of %s: %s", package, exc)
        return {}


async def package_context(
    http: ReadOnlyHTTP | None, package: str, series: dict[str, dict[str, Any]]
) -> PackageContext:
    context = PackageContext(package=package)
    if current := current_versions(series):
        context.native = all(Version(v).debian_revision is None for v in current.values())
    if http is not None:
        try:
            context.upstream = await read_upstream_info(http, package)
        except ToolError as exc:
            logger.info("No upstream information for %s: %s", package, exc)
    return context


def filed(payload: dict[str, Any]) -> datetime | None:
    """When the bug was reported."""
    try:
        when = datetime.fromisoformat(payload.get("date_created") or "")
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


async def uploads_since(lp: Any, package: str, since: datetime | None) -> list[Upload]:
    if lp is None or since is None:
        return []
    try:
        uploads = await in_lp_thread(recent_uploads, lp, package, since)
    except Exception as exc:
        logger.warning("Could not read the recent uploads of %s: %s", package, exc)
        return []
    return [Upload(**u | {"published": u["published"].date().isoformat()}) for u in uploads]


async def suspects(
    http: ReadOnlyHTTP | None,
    package: str,
    facts: BugFacts,
    affected: set[str],
    context: PackageContext,
    series: dict[str, dict[str, Any]],
) -> list[Upload]:
    """Updates that may have caused the bug, each with the version before it."""
    if http is None:
        return []
    found = [
        u
        for u in context.recent_uploads
        if UPDATE_POCKETS & set(u.pockets) and (not affected or u.series in affected)
    ]
    if not found and facts.since_update:
        # Reported after an update that is older than the recent uploads window.
        for name, row in series.items():
            published = row.get("published", {})
            pockets = [p for p in UPDATE_POCKETS if p in published]
            if pockets and (not affected or name in affected):
                newest = max((published[p] for p in pockets), key=Version)
                found.append(Upload(series=name, version=newest, pockets=pockets, published=""))
    with_previous = []
    for upload in found[:MAX_SUSPECTS]:
        upload.previous = await previous_version(http, package, upload.version)
        if upload.previous:
            with_previous.append(upload)
    return with_previous


@dataclass
class Regression:
    """The regression check of one bug: the suspects, and what the model concluded."""

    suspects: list[Upload]
    result: RegressionCheck | None = None

    @property
    def confirmed(self) -> RegressionCheck | None:
        return self.result if self.result is not None and self.result.confirmed else None

    @property
    def update(self) -> Upload | None:
        """The confirmed suspect, if it was published as an update."""
        if self.confirmed is None:
            return None
        for upload in self.suspects:
            if upload.version == self.confirmed.suspect_version and UPDATE_POCKETS & set(upload.pockets):
                return upload
        return None
