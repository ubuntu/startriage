"""Launchpad read tools, using the user's authenticated launchpadlib session.

launchpadlib is blocking, so every call runs in a worker thread. It is not
thread-safe either (parallel calls corrupt its shared TLS connection), so the
calls are serialized. Private bugs are refused: their content must not be sent
to the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ...sources.launchpad.finder import NOWORK_BUG_STATUSES, TRACKED_BUG_STATUSES
from .base import MAX_TOOL_OUTPUT, Risk, ToolContext, ToolDef, ToolError, int_arg, page_text, parse_bug_arg

logger = logging.getLogger(__name__)

MAX_COMMENTS = 30
MAX_COMMENT_CHARS = 3000
MAX_SEARCH_RESULTS = 25
MAX_ATTACHMENT = 5_000_000
# Payload keys used by the host only (precheck), never sent to the model.
HOST_ONLY_KEYS = ("private", "information_type", "expiring", "activity")

_LP_LOCK = threading.Lock()


async def in_lp_thread[T](fn: Callable[..., T], *args: Any) -> T:
    """Run a blocking launchpadlib call in a worker thread, one at a time."""

    def locked() -> T:
        with _LP_LOCK:
            return fn(*args)

    return await asyncio.to_thread(locked)


def _lp(ctx: ToolContext) -> Any:
    if ctx.lp is None:
        raise ToolError("Launchpad is not available in this run")
    return ctx.lp


def _public_bug(lp: Any, number: int) -> Any:
    try:
        bug = lp.bugs[number]
    except KeyError:
        raise ToolError(f"bug {number} does not exist") from None
    if bug.private:
        raise ToolError(f"bug {number} is private; private bugs are not available")
    return bug


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1)


def model_payload(
    payload: dict[str, Any], *, before: int = 0, max_chars: int | None = None
) -> dict[str, Any]:
    """Return ``payload`` as sent to the model: no host-only keys, the latest comments only.

    Comments are numbered from 1; ``before`` keeps only those before that number
    (to page back), and ``max_chars`` drops the oldest ones until the JSON fits.
    """
    payload = {key: value for key, value in payload.items() if key not in HOST_ONLY_KEYS}
    comments: list[dict[str, Any]] = [
        {**comment, "n": n} for n, comment in enumerate(payload.get("comments", []), start=1)
    ]
    if before > 0:
        comments = comments[: before - 1]
    for comment in comments:
        if len(comment.get("text") or "") > MAX_COMMENT_CHARS:
            comment["text"] = comment["text"][:MAX_COMMENT_CHARS] + " [comment truncated]"
    kept = comments[-MAX_COMMENTS:]
    payload["comments"] = kept
    while max_chars and len(kept) > 1 and len(_dumps(payload)) > max_chars:
        kept = kept[1:]
        payload["comments"] = kept
    if omitted := len(comments) - len(kept):
        logger.info("Bug %s: %d older comments left out", payload.get("number"), omitted)
        payload["comments_omitted"] = omitted
        if max_chars:
            payload["older_comments"] = f"call lp_get_bug with before={kept[0]['n']} to read them"
    return payload


def _get_bug(lp: Any, number: int, before: int) -> str:
    from ...sources.launchpad.models import Task

    bug = _public_bug(lp, number)
    tasks = list(bug.bug_tasks)
    if not tasks:
        raise ToolError(f"bug {number} has no tasks")
    payload = Task(tasks[0], last_activity_ours=False).to_agent_payload()
    return _dumps(model_payload(payload, before=before, max_chars=MAX_TOOL_OUTPUT))


async def lp_get_bug(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await in_lp_thread(_get_bug, _lp(ctx), parse_bug_arg(args), int_arg(args, "before"))


def _get_attachment(lp: Any, number: int, index: int) -> str:
    bug = _public_bug(lp, number)
    attachments = list(bug.attachments)
    if not 0 <= index < len(attachments):
        raise ToolError(f"bug {number} has {len(attachments)} attachments (index 0-{len(attachments) - 1})")
    attachment = attachments[index]
    with attachment.data.open() as f:
        data = f.read(MAX_ATTACHMENT + 1)
    if b"\0" in data[:8192]:
        raise ToolError(f"attachment {index} ({attachment.title!r}) is binary")
    text = data[:MAX_ATTACHMENT].decode("utf-8", errors="replace")
    if len(data) > MAX_ATTACHMENT:
        text += "\n[attachment truncated]"
    return text


async def lp_get_attachment(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        index = int(args.get("index", -1))
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        raise ToolError("index and offset must be integers") from None
    text = await in_lp_thread(_get_attachment, _lp(ctx), parse_bug_arg(args), index)
    return page_text(text, pattern=args.get("pattern"), offset=offset)


def search_tasks(
    lp: Any, package: str, text: str, include_closed: bool, limit: int = MAX_SEARCH_RESULTS
) -> list[dict[str, Any]]:
    """Search the public bugs of an Ubuntu source package (blocking; use :func:`in_lp_thread`)."""
    statuses = TRACKED_BUG_STATUSES + (NOWORK_BUG_STATUSES if include_closed else [])
    source = lp.distributions["ubuntu"].getSourcePackage(name=package)
    if source is None:
        raise ToolError(f"no Ubuntu source package named {package!r}")
    tasks = source.searchTasks(
        search_text=text,
        status=statuses,
        information_type=["Public", "Public Security"],
        omit_duplicates=False,
    )
    return [
        {
            "bug": task.bug_link.rsplit("/", 1)[-1],
            "title": task.title,
            "status": task.status,
            "importance": task.importance,
            "created": task.date_created.date().isoformat() if task.date_created else None,
        }
        for task in tasks[:limit]
    ]


def _search_bugs(lp: Any, package: str, text: str, include_closed: bool) -> str:
    results = search_tasks(lp, package, text, include_closed)
    if not results:
        return f"No {package} bugs match {text!r}."
    return json.dumps(results, ensure_ascii=False, indent=1)


_ACTIVE_SERIES: dict[int, list[dict[str, Any]]] = {}


def _active_series(lp: Any) -> list[dict[str, Any]]:
    """Active Ubuntu series, development first (cached per Launchpad session)."""
    if id(lp) not in _ACTIVE_SERIES:
        ubuntu = lp.distributions["ubuntu"]
        devel = ubuntu.current_series.name
        _ACTIVE_SERIES[id(lp)] = [
            {"name": s.name, "version": s.version, "status": s.status, "development": s.name == devel}
            for s in ubuntu.series_collection
            if s.active
        ]
    return _ACTIVE_SERIES[id(lp)]


def series_table(lp: Any, package: str) -> dict[str, dict[str, Any]]:
    """Version of ``package`` per pocket in each active Ubuntu series (blocking).

    Series where the package is not published have an empty ``published``.
    """
    table = {
        s["name"]: {
            "version": s["version"],
            "status": s["status"],
            "development": s["development"],
            "published": {},
        }
        for s in _active_series(lp)
    }
    archive = lp.distributions["ubuntu"].main_archive
    for pub in archive.getPublishedSources(source_name=package, exact_match=True, status="Published"):
        row = table.get(pub.distro_series_link.rsplit("/", 1)[-1])
        if row is not None:
            # Newest first: keep the first version seen per pocket.
            row["published"].setdefault(pub.pocket, pub.source_package_version)
    return table


def recent_uploads(lp: Any, package: str, until: datetime, days: int = 30) -> list[dict[str, Any]]:
    """Versions of ``package`` published in the ``days`` before ``until`` (blocking).

    Proposed is left out: a regression reported by users is in a pocket they
    install from. Newest first; one entry per series and version.
    """
    since = until - timedelta(days=days)
    archive = lp.distributions["ubuntu"].main_archive
    uploads: dict[tuple[str, str], dict[str, Any]] = {}
    for pub in archive.getPublishedSources(source_name=package, exact_match=True, created_since_date=since):
        published = pub.date_published
        if pub.pocket == "Proposed" or published is None or not since <= published <= until:
            continue
        series = pub.distro_series_link.rsplit("/", 1)[-1]
        entry = uploads.setdefault(
            (series, pub.source_package_version),
            {"series": series, "version": pub.source_package_version, "pockets": [], "published": published},
        )
        if pub.pocket not in entry["pockets"]:
            entry["pockets"].append(pub.pocket)
        entry["published"] = min(entry["published"], published)
    return sorted(uploads.values(), key=lambda u: u["published"], reverse=True)


async def pkg_versions(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = str(args.get("package", "")).strip()
    if not package:
        raise ToolError("package is required")
    table = await in_lp_thread(series_table, _lp(ctx), package)
    if not any(row["published"] for row in table.values()):
        return f"{package} is not published in any active Ubuntu series."
    return json.dumps(table, ensure_ascii=False, indent=1)


async def lp_search_bugs(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = str(args.get("package", "")).strip()
    text = str(args.get("text", "")).strip()
    if not package or not text:
        raise ToolError("package and text are required")
    include_closed = bool(args.get("include_closed", False))
    return await in_lp_thread(_search_bugs, _lp(ctx), package, text, include_closed)


_BUG_PARAM = {"type": "integer", "description": "Launchpad bug number"}

TOOLS = [
    ToolDef(
        name="lp_get_bug",
        group="launchpad",
        risk=Risk.read,
        default=True,
        summary="Read a public Launchpad bug (description, comments, attachment list)",
        description=(
            "Read a public Launchpad bug: title, description, status per affected package, "
            "tags, duplicate-of, attachment list (with index) and the latest comments "
            "(numbered; give 'before' to read the ones before a number)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "bug": _BUG_PARAM,
                "before": {"type": "integer", "description": "Only comments before this number"},
            },
            "required": ["bug"],
        },
        run=lp_get_bug,
    ),
    ToolDef(
        name="lp_get_attachment",
        group="launchpad",
        risk=Risk.read,
        default=True,
        summary="Read a text attachment of a public Launchpad bug (paged or grepped)",
        description=(
            "Read a text attachment of a public Launchpad bug. Returns a page of lines from "
            "'offset', or only the lines matching the case-insensitive regex 'pattern'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "bug": _BUG_PARAM,
                "index": {"type": "integer", "description": "Attachment index from lp_get_bug"},
                "pattern": {"type": "string", "description": "Regex; return only matching lines"},
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
            },
            "required": ["bug", "index"],
        },
        run=lp_get_attachment,
    ),
    ToolDef(
        name="lp_search_bugs",
        group="launchpad",
        risk=Risk.read,
        default=True,
        summary="Search public Launchpad bugs of an Ubuntu source package",
        description=(
            "Search the public Launchpad bugs of an Ubuntu source package by text. "
            "Open bugs only, unless include_closed is true (Fix Released, Invalid, Won't Fix, ...). "
            "Use a few distinctive words (e.g. from the error message), not the whole title."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Ubuntu source package name"},
                "text": {"type": "string", "description": "Search words"},
                "include_closed": {"type": "boolean", "description": "Also return closed bugs"},
            },
            "required": ["package", "text"],
        },
        run=lp_search_bugs,
    ),
    ToolDef(
        name="pkg_versions",
        group="launchpad",
        risk=Risk.read,
        default=True,
        summary="Show the versions of an Ubuntu source package in each supported series",
        description=(
            "Show the published version of an Ubuntu source package per pocket (Release, Updates, "
            "Security, Proposed) in each active Ubuntu series, and which series is in development."
        ),
        parameters={
            "type": "object",
            "properties": {"package": {"type": "string", "description": "Ubuntu source package name"}},
            "required": ["package"],
        },
        run=pkg_versions,
    ),
]
