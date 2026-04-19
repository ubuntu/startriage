"""Launchpad Task model for startriage.

Ported from ustriage/task.py with:
- _sibling_tasks cached via @lru_cache (was recomputed on each call)
- _is_in_unapproved removed from the model; unapproved check is done
  in bulk in finder.py (one getPackageUploads call per series, not per bug)
- get_changelog_versions made async-ready (called by finder after LP fetch)
"""

from __future__ import annotations

import re
import urllib
import urllib.request
from functools import lru_cache
from typing import Any, ClassVar

import debian.deb822

DISTRIBUTION_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#distribution"
DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK = (
    "https://api.launchpad.net/devel/#distribution_source_package"
)
SOURCE_PACKAGE_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#source_package"
PROJECT_RESOURCE_TYPE_LINK = "https://api.launchpad.net/devel/#project"

COLOR_CYAN = "\033[0;36m"
COLOR_GREEN = "\033[0;32m"
COLOR_YELLOW = "\033[0;33m"
COLOR_RESET = "\033[0m"
STR_STRIKETHROUGH = "\u0336"

BUG_URL_BASE = "https://bugs.launchpad.net/ubuntu/+bug/"
LPBUGREF = "LP: #"


def truncate_string(text: str, length: int = 20) -> str:
    s = str(text)
    if len(s) > length:
        return s[: length - 1] + "…"
    return s


def mark(text: str, color: str) -> str:
    return color + text + COLOR_RESET


def _find_changes_bugs(changes_url: str) -> list[str]:
    with urllib.request.urlopen(changes_url) as fobj:
        changes = debian.deb822.Changes(fobj)
    try:
        return changes["Launchpad-Bugs-Fixed"].split()
    except KeyError:
        return []


class Task:
    """Launchpad Bug Task with cached properties and bulk unapproved support."""

    # Class-level state set by finder before rendering
    LP = None
    NOWORK_BUG_STATUSES: ClassVar[list[str]] = []
    OPEN_BUG_STATUSES: ClassVar[list[str]] = []
    AGE = None  # datetime: threshold for U flag
    OLD = None  # datetime: threshold for O flag

    # Set by finder after bulk unapproved lookup: {(bug_number, src): bool}
    _unapproved_cache: ClassVar[dict[tuple[str, str], bool]] = {}

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
        return BUG_URL_BASE + self.number

    @property
    def bug_reference(self) -> str:
        return LPBUGREF + self.number

    @property
    @lru_cache(maxsize=None)  # noqa: B019
    def _sibling_tasks(self) -> dict[str, Any]:
        """All sibling tasks for this package across series - cached."""
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

    def is_in_unapproved(self) -> bool:
        """Check bulk unapproved cache populated by finder."""
        return Task._unapproved_cache.get((self.number, self.src), False)

    def get_releases(self, length: int) -> str:
        info = ""
        for series, lp_task in self._sibling_tasks.items():
            char = "D" if series[0] == "-" else series[0].upper()
            if lp_task.status in Task.NOWORK_BUG_STATUSES:
                char = mark(char, COLOR_GREEN)
            elif self.is_in_unapproved():
                char = mark(char, COLOR_CYAN)
            elif lp_task.status in Task.OPEN_BUG_STATUSES:
                char = mark(char, COLOR_YELLOW)
            info += char

        printable_len = len(re.sub("[^A-Z]+", "", info))
        if length > printable_len:
            info += " " * (length - printable_len)
        return info

    def _is_updated(self) -> bool:
        return bool(Task.AGE and self.date_last_updated > Task.AGE)

    def _is_old(self) -> bool:
        return bool(Task.OLD and self.date_last_updated < Task.OLD)

    def _is_verification_needed(self) -> bool:
        return any("verification-needed-" in t for t in self.tags)

    def _is_verification_done(self) -> bool:
        return any("verification-done-" in t for t in self.tags)

    def get_flags(self, newbug: bool = False) -> str:
        v_needed = mark("v", COLOR_CYAN)
        v_done = mark("V", COLOR_GREEN)
        return (
            ("*" if self.subscribed else " ")
            + ("+" if self.last_activity_ours else " ")
            + ("U" if self._is_updated() else "O" if self._is_old() else " ")
            + ("N" if newbug else " ")
            + (v_needed if self._is_verification_needed() else " ")
            + (v_done if self._is_verification_done() else " ")
        )

    def compose_pretty(
        self, bugid_len: int, shortlinks: bool = True, extended: bool = False, newbug: bool = False
    ) -> str:
        bug_ref = self.bug_reference if shortlinks else self.url
        fmt_len = bugid_len + len(LPBUGREF if shortlinks else BUG_URL_BASE)
        bug_str = f"%-{fmt_len}s" % bug_ref

        text = "%-12s | %6s | %-7s | %-13s | %-19s |" % (
            bug_str,
            self.get_flags(newbug),
            self.get_releases(7),
            self.status,
            truncate_string(self.src, 19),
        )
        if extended:
            text += " %8s | %-10s | %-13s |" % (
                self.date_last_updated.strftime("%d.%m.%y"),
                self.importance,
                truncate_string(self.assignee or "", 12),
            )
        text += " %-60s |" % truncate_string(self.short_title, 60)
        return text

    def compose_dup(self, extended: bool = False) -> str:
        text = f"{self.status},{truncate_string(self.src, 16)}"
        if extended and self.assignee:
            text += f",{truncate_string(self.assignee, 9)}"
        return text

    @staticmethod
    def get_header(extended: bool = False) -> str:
        text = "%-12s | %-6s | %-7s | %-13s | %-19s |" % ("Bug", "Flags", "Release", "Status", "Package")
        if extended:
            text += " %-8s | %-10s | %-13s |" % ("Last Upd", "Prio", "Assignee")
        text += " %-60s |" % "Title"
        return text

    def sort_key(self):
        return (not self.last_activity_ours, self.number, self.src)

    def sort_date(self):
        return self.date_last_updated

    @lru_cache(maxsize=None)  # noqa: B019
    def to_dict(self) -> dict:
        sibling_status = {}
        for series, lp_task in self._sibling_tasks.items():
            if lp_task.status in Task.NOWORK_BUG_STATUSES:
                sibling_status[series] = "closed"
            elif self.is_in_unapproved():
                sibling_status[series] = "unapproved"
            elif lp_task.status in Task.OPEN_BUG_STATUSES:
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
            "is_updated_recently": self._is_updated(),
            "is_old": self._is_old(),
            "is_verification_needed": self._is_verification_needed(),
            "is_verification_done": self._is_verification_done(),
            "sibling_task_status": sibling_status,
        }
