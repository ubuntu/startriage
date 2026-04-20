"""Launchpad Task model for startriage.

Ported from ustriage/task.py with these improvements:
- _sibling_tasks cached via @lru_cache (was recomputed on each call)
- Unapproved-queue check done in bulk in finder.py (one getPackageUploads()
  per series, not per bug); result passed via RenderContext, not class state.
- All mutable rendering state (bug statuses, unapproved cache, age thresholds)
  is passed explicitly via RenderContext — no class-level injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from launchpadlib.launchpad import Launchpad

DISTRIBUTION_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#distribution"
DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK = (
    "https://api.launchpad.net/devel/#distribution_source_package"
)
SOURCE_PACKAGE_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#source_package"
PROJECT_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#project"

COLOR_STATUS_WORKNEEDED = "\033[0;34m"  # blue
COLOR_STATUS_NOWORK = "\033[0;32m"  # green
COLOR_STATUS_OPEN = "\033[0;31m"  # red
COLOR_RESET = "\033[0m"
STR_STRIKETHROUGH = "\u0336"

LONG_URL_ROOT = "https://bugs.launchpad.net/ubuntu/+bug/"
SHORTLINK_ROOT = "https://pad.lv/"
LPBUGREF = "LP: #"


def truncate_string(text: str, length: int = 20) -> str:
    s = str(text)
    if len(s) > length:
        return s[: length - 1] + "\u2026"
    return s


def mark(text: str, color: str) -> str:
    return color + text + COLOR_RESET


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
    age: datetime | None = None
    old: datetime | None = None


class Task:
    """Launchpad Bug Task with cached properties.

    Mutable rendering state (bug status lists, unapproved cache, age thresholds)
    is NOT stored on this class -- it is passed via RenderContext to display methods.
    """

    def __init__(self, lp_task=None) -> None:
        self.subscribed: bool | None = None
        self.last_activity_ours: bool | None = None

        self.obj: Any
        if lp_task:
            parts = str(lp_task).split("/")
            self.distro = parts[4]
            self.source_package_name = parts[-3]
            self.series = parts[5] if parts[5] != "+source" else "-devel"
            self.obj = lp_task
        else:
            self.distro = None
            self.source_package_name = None
            self.series = None
            self.obj = None

    def __str__(self) -> str:
        return f"LP #{self.number:8} {self.status:12} {self.title}"

    @staticmethod
    def create_from_launchpadlib_object(obj, **kwargs) -> Task:
        self = Task()
        self.obj = obj
        parts = str(obj).split("/")
        self.distro = parts[4]
        self.source_package_name = parts[-3]
        self.series = parts[5] if parts[5] != "+source" else "-devel"
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def number(self) -> str:
        return self.title.split(" ")[1].replace("#", "")

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def title(self) -> str:
        return self.obj.title

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
        }.get(self.obj.target.resource_type_link, 4)
        return " ".join(self.title.split(" ")[start_field:]).replace('"', "")

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def src(self) -> str:
        return self.title.split(" ")[3]

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def status(self) -> str:
        return self.obj.status

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def importance(self) -> str:
        return self.obj.importance

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def tags(self) -> list[str]:
        return self.obj.bug.tags

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def assignee(self) -> str | None:
        if self.obj.assignee_link:
            return self.obj.assignee_link.split("~")[1]
        return None

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def date_last_updated(self):
        return self.obj.bug.date_last_updated

    @property
    def url(self) -> str:
        return LONG_URL_ROOT + self.number

    @property
    def shortlink(self) -> str:
        return SHORTLINK_ROOT + self.number

    @property
    def bug_reference(self) -> str:
        return LPBUGREF + self.number

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def _sibling_tasks(self) -> dict[str, Any]:
        """All sibling tasks for this package across series -- cached."""
        siblings = {}
        for lp_task in self.obj.bug.bug_tasks:
            parts = str(lp_task).split("/")
            if parts[4] != "ubuntu":
                continue
            if parts[-3] != str(self.src):
                continue
            series = parts[5] if parts[5] != "+source" else "-devel"
            siblings[series] = lp_task
        return siblings

    def is_in_unapproved(self, ctx: RenderContext) -> bool:
        """Check bulk unapproved cache from RenderContext."""
        return ctx.unapproved_cache.get((self.number, self.src), False)

    def _is_updated(self, ctx: RenderContext) -> bool:
        return bool(ctx.age and self.date_last_updated > ctx.age)

    def _is_old(self, ctx: RenderContext) -> bool:
        return bool(ctx.old and self.date_last_updated < ctx.old)

    def _is_verification_needed(self) -> bool:
        return any("verification-needed-" in t for t in self.tags)

    def _is_verification_done(self) -> bool:
        return any("verification-done-" in t for t in self.tags)

    def get_releases(self, ctx: RenderContext, length: int) -> str:
        info = ""
        for series, lp_task in self._sibling_tasks.items():
            char = "D" if series[0] == "-" else series[0].upper()
            if lp_task.status in ctx.nowork_statuses:
                char = mark(char, COLOR_STATUS_NOWORK)
            elif self.is_in_unapproved(ctx):
                char = mark(char, COLOR_STATUS_WORKNEEDED)
            elif lp_task.status in ctx.open_statuses:
                char = mark(char, COLOR_STATUS_OPEN)
            info += char

        printable_len = len(re.sub("[^A-Z]+", "", info))
        if length > printable_len:
            info += " " * (length - printable_len)
        return info

    def get_flags(self, ctx: RenderContext, newbug: bool = False) -> str:
        v_needed = mark("v", COLOR_STATUS_WORKNEEDED)
        v_done = mark("V", COLOR_STATUS_NOWORK)
        return (
            ("*" if self.subscribed else " ")
            + ("+" if not self.last_activity_ours else " ")
            + ("U" if self._is_updated(ctx) else "O" if self._is_old(ctx) else " ")
            + ("N" if newbug else " ")
            + (v_needed if self._is_verification_needed() else " ")
            + (v_done if self._is_verification_done() else " ")
        )

    @staticmethod
    def get_header(extended: bool = False) -> str:
        text = "%-12s | %-6s | %-7s | %-13s | %-19s |" % ("Bug", "Flags", "Release", "Status", "Package")
        if extended:
            text += " %-8s | %-10s | %-13s |" % ("Last Upd", "Prio", "Assignee")
        text += " %-73s |" % "Title"
        return text

    def get_line(
        self,
        ctx: RenderContext,
        bugid_len: int,
        shortlinks: bool = True,
        extended: bool = False,
        newbug: bool = False,
    ) -> str:
        bug_ref = self.bug_reference if shortlinks else self.url
        fmt_len = bugid_len + len(LPBUGREF if shortlinks else LONG_URL_ROOT)
        bug_str = f"%-{fmt_len}s" % bug_ref

        text = "%-12s | %6s | %-7s | %-13s | %-19s |" % (
            bug_str,
            self.get_flags(ctx, newbug),
            self.get_releases(ctx, 7),
            self.status,
            truncate_string(self.src, 19),
        )
        if extended:
            text += " %8s | %-10s | %-13s |" % (
                self.date_last_updated.strftime("%d.%m.%y"),
                self.importance,
                truncate_string(self.assignee or "", 12),
            )
        text += " %-73s |" % truncate_string(self.short_title, 73)
        return text

    def compose_dup(self, extended: bool = False) -> str:
        text = f"{truncate_string(self.src, 16)}:{self.status}"
        if extended and self.assignee:
            text += f"@{truncate_string(self.assignee, 9)}"
        return text

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
