"""Launchpad Task model for startriage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from launchpadlib.launchpad import Launchpad

from startriage.output import hyperlink, truncate_string

DISTRIBUTION_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#distribution"
DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK = (
    "https://api.launchpad.net/devel/#distribution_source_package"
)
SOURCE_PACKAGE_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#source_package"
PROJECT_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#project"

COLOR_STATUS_WAITOTHER = "\033[0;34m"  # blue
COLOR_STATUS_DONE = "\033[0;32m"  # green
COLOR_STATUS_OPEN = "\033[0;31m"  # red
COLOR_RESET = "\033[0m"

LONG_URL_ROOT = "https://bugs.launchpad.net/ubuntu/+bug/"
LPBUGREF = "LP: #"

# Visual width of the Release column in the triage table.
_RELEASE_COL_WIDTH = 7


def mark(text: str, color: str) -> str:
    return "".join([color, text, COLOR_RESET])


@dataclass
class RenderContext:
    """Render-time state passed explicitly to Task display methods.

    Avoids class-level mutation; each triage run can have its own context.
    """

    nowork_statuses: list[str] = field(default_factory=list)
    open_statuses: list[str] = field(default_factory=list)
    # {(bug_number, src_package): True} -- populated by finder bulk unapproved check
    unapproved_cache: dict[tuple[str, str], bool] = field(default_factory=dict)
    # datetime thresholds for U (recently updated) and O (old) flags
    recent_since: datetime | None = None
    old_since: datetime | None = None


class Task:
    """Launchpad Bug Task with cached properties.

    Mutable rendering state (bug status lists, unapproved cache, age thresholds)
    is NOT stored on this class -- it is passed via RenderContext to display methods.
    """

    def __init__(
        self, lp_task: Any, subscribed: bool, last_activity_ours: bool, expiring: bool = False
    ) -> None:
        self.subscribed: bool | None = subscribed
        self.last_activity_ours: bool | None = last_activity_ours
        self.expiring: bool = expiring

        parts = str(lp_task).split("/")
        self.distro = parts[4]
        self.source_package_name = parts[-3]
        self.series = parts[5] if parts[5] != "+source" else "-devel"
        self.title: str = lp_task.title
        self.number: str = self.title.split(" ")[1].replace("#", "")
        self.status: str = lp_task.status
        self.importance: str = lp_task.importance
        self.src: str = self.title.split(" ")[3]
        self.tags: list[str] = lp_task.bug.tags
        self.date_last_updated = lp_task.bug.date_last_updated
        if lp_task.assignee_link:
            self.assignee: str | None = lp_task.assignee_link.split("~")[1]
        else:
            self.assignee = None
        self.lp_task = lp_task

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return (
            self.number == other.number
            and self.src == other.src
            and self.distro == other.distro
            and self.series == other.series
        )

    def __hash__(self) -> int:
        return hash((self.number, self.src, self.distro, self.series))

    def __str__(self) -> str:
        return f"LP #{self.number:8} {self.status:12} {self.title}"

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def short_title(self) -> str:
        """Bug title stripped of the leading LP task prefix.

        LP task titles follow the pattern:
          "Bug #NNN in <target>: <actual title>"

        The word offset at which the actual title starts depends on the target type:
          - distribution (ubuntu):                    word 4   "Bug #N in ubuntu: ..."
          - distribution_source_package (ubuntu/pkg): word 5   "Bug #N in pkg (ubuntu): ..."
          - source_package (series/pkg):              word 6   "Bug #N in pkg (ubuntu/series): ..."
          - project:                                  word 7   "Bug #N in project (display): ..."
        """
        start_field = {
            DISTRIBUTION_RESOURCE_TYPE_LINK: 4,
            DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK: 5,
            SOURCE_PACKAGE_RESOURCE_TYPE_LINK: 6,
            PROJECT_RESOURCE_TYPE_LINK: 7,
        }.get(self.lp_task.target.resource_type_link, 4)
        return " ".join(self.title.split(" ")[start_field:]).replace('"', "")

    @property
    def url(self) -> str:
        return LONG_URL_ROOT + self.number

    @property
    def bug_reference(self) -> str:
        return LPBUGREF + self.number

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def _sibling_tasks(self) -> dict[str, Any]:
        """All sibling tasks for this package across series -- cached.

        Order: devel first, then stable series newest-first (LP API order reversed).
        """
        siblings = {}
        for lp_task in self.lp_task.bug.bug_tasks:
            parts = str(lp_task).split("/")
            if parts[4] != "ubuntu":
                continue
            if parts[-3] != str(self.src):
                continue
            series = parts[5] if parts[5] != "+source" else "-devel"
            siblings[series] = lp_task
        devel = {k: v for k, v in siblings.items() if k.startswith("-")}
        stable = {k: v for k, v in siblings.items() if not k.startswith("-")}
        return devel | dict(reversed(stable.items()))

    def is_in_unapproved(self, ctx: RenderContext) -> bool:
        """Check bulk unapproved cache from RenderContext."""
        return ctx.unapproved_cache.get((self.number, self.src), False)

    def _is_updated(self, ctx: RenderContext) -> bool:
        return bool(ctx.recent_since and self.date_last_updated > ctx.recent_since)

    def _is_old(self, ctx: RenderContext) -> bool:
        return bool(ctx.old_since and self.date_last_updated < ctx.old_since)

    def _is_verification_needed(self) -> bool:
        return any("verification-needed-" in t for t in self.tags)

    def _is_verification_done(self) -> bool:
        return any("verification-done-" in t for t in self.tags)

    def _release_chars(self, ctx: RenderContext) -> list[str]:
        """Return one element per active series task.
        Each element is one distro release, and the text can contain an ANSI color code.
        Order: devel first, then stable series newest-first.
        """
        chars = []
        for series, lp_task in self._sibling_tasks.items():
            char = "D" if series[0] == "-" else series[0].upper()
            if lp_task.status in ctx.nowork_statuses:
                char = mark(char, COLOR_STATUS_DONE)
            elif self.is_in_unapproved(ctx):
                char = mark(char, COLOR_STATUS_WAITOTHER)
            elif lp_task.status in ctx.open_statuses:
                char = mark(char, COLOR_STATUS_OPEN)
            chars.append(char)
        return chars

    def release_tasks_str(self, ctx: RenderContext, width: int = 0) -> str:
        chars = self._release_chars(ctx)
        return "".join(chars) + " " * max(0, width - len(chars))

    def get_flags(self, ctx: RenderContext, newbug: bool = False) -> str:
        v_needed = mark("v", COLOR_STATUS_WAITOTHER)
        v_done = mark("V", COLOR_STATUS_DONE)
        return (
            ("*" if self.subscribed else " ")
            + ("+" if not self.last_activity_ours else " ")
            + ("U" if self._is_updated(ctx) else "X" if self.expiring else "O" if self._is_old(ctx) else " ")
            + ("N" if newbug else " ")
            + (v_needed if self._is_verification_needed() else " ")
            + (v_done if self._is_verification_done() else " ")
        )

    @staticmethod
    def get_table_header(extended: bool = False) -> str:
        text = "%-12s | %-6s | %-*s | %-13s | %-19s |" % (
            "Bug",
            "Flags",
            _RELEASE_COL_WIDTH,
            "Release",
            "Status",
            "Package",
        )
        if extended:
            text += " %-8s | %-10s | %-13s |" % ("Last Upd", "Prio", "Assignee")
        text += " %-60s |" % "Title"
        return text

    def get_table_row(
        self,
        ctx: RenderContext,
        bugid_len: int,
        shortlinks: bool = True,
        extended: bool = False,
        newbug: bool = False,
    ) -> str:
        bug_ref = self.bug_reference if shortlinks else self.url
        fmt_len = bugid_len + len(LPBUGREF if shortlinks else LONG_URL_ROOT)
        bug_str = hyperlink(self.url, f"%-{fmt_len}s" % bug_ref)

        # split up distro tasks to multiple lines if necessary.
        release_chars = self._release_chars(ctx)
        chunks = [
            release_chars[i : i + _RELEASE_COL_WIDTH]
            for i in range(0, len(release_chars), _RELEASE_COL_WIDTH)
        ] or [[]]

        def _release_col(chunk: list[str]) -> str:
            return "".join(chunk) + " " * (_RELEASE_COL_WIDTH - len(chunk))

        text = "%-12s | %6s | %s | %-13s | %-19s |" % (
            bug_str,
            self.get_flags(ctx, newbug),
            _release_col(chunks[0]),
            self.status,
            truncate_string(self.src, 19),
        )
        if extended:
            text += " %8s | %-10s | %-13s |" % (
                self.date_last_updated.strftime("%y-%m-%d"),
                self.importance,
                truncate_string(self.assignee or "", 12),
            )
        text += " %-60s |" % truncate_string(self.short_title, 60)

        if len(chunks) > 1:
            # Continuation rows: blank out everything except the release column.
            pre = " " * fmt_len + " | " + " " * 6 + " | "
            post = " | %-13s | %-19s |" % ("", "")
            if extended:
                post += " %8s | %-10s | %-13s |" % ("", "", "")
            post += " %-60s |" % ""
            for chunk in chunks[1:]:
                text += "\n" + pre + _release_col(chunk) + post

        return text

    def compose_dup(self, extended: bool = False) -> str:
        text = f"{truncate_string(self.src, 16)}:{self.status}"
        if extended and self.assignee:
            text += f"@{truncate_string(self.assignee, 9)}"
        return text

    def actionability_rank(self, ctx: RenderContext) -> int:
        """Lower = more actionable; used to pick the primary row when a bug
        has multiple tasks.

        0 — status is in open_statuses (needs work)
        1 — status is neither open nor done (e.g. Fix Committed, Incomplete)
        2 — status is in nowork_statuses (won't fix, invalid, fix released, …)
        """
        if self.status in ctx.open_statuses:
            return 0
        if self.status in ctx.nowork_statuses:
            return 2
        return 1

    def sort_key(self):
        return (not self.last_activity_ours, self.number, self.src)

    def sort_date(self):
        return self.date_last_updated

    def to_dict(self, ctx: RenderContext) -> dict:
        sibling_status = {}
        for series, lp_task in self._sibling_tasks.items():
            if lp_task.status in ctx.nowork_statuses:
                sibling_status[series] = "closed"
            elif self.is_in_unapproved(ctx):
                sibling_status[series] = "unapproved"
            elif lp_task.status in ctx.open_statuses:
                sibling_status[series] = "open"
            else:
                sibling_status[series] = "pending"
        return {
            "url": self.url,
            "bug_reference": self.bug_reference,
            "number": self.number,
            "title": self.title,
            "short_title": self.short_title,
            "distro": self.distro,
            "source_package": self.src,
            "source_package_name": self.source_package_name,
            "series": self.series,
            "importance": self.importance,
            "status": self.status,
            "tags": self.tags,
            "assignee": self.assignee,
            "is_maintainer_subscribed": self.subscribed,
            "is_last_activity_by_maintainer": self.last_activity_ours,
            "is_updated_recently": self._is_updated(ctx),
            "is_old": self._is_old(ctx),
            "is_verification_needed": self._is_verification_needed(),
            "is_verification_done": self._is_verification_done(),
            "sibling_task_status": sibling_status,
        }


@dataclass
class LaunchpadTasks:
    tasks: list[Task]
    lp: Launchpad

    changes_pairs: list[tuple[str, str]] = field(default_factory=list)
    nowork_statuses: list[str] = field(default_factory=list)
    open_statuses: list[str] = field(default_factory=list)
    expiring_tagged: list[Task] = field(default_factory=list)
    expiring_subscribed: list[Task] = field(default_factory=list)
