"""Shared output helpers for startriage."""

from __future__ import annotations

import os
import sys
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, NamedTuple

from .savebugs import BugPersistor


class OutputFormat(StrEnum):
    TERMINAL = "terminal"
    MARKDOWN = "markdown"
    JSON = "json"


@dataclass
class OutputConfig:
    fmt: OutputFormat
    out: IO[str]
    open_in_browser: bool = False
    terminal_links: bool = True
    bug_persistor: BugPersistor | None = None
    markdown_path: Path | None = None
    html_path: Path | None = None


class Flag(StrEnum):
    SUBSCRIBED = "subscribed"
    EXTERNAL = "external"  # last activity not by the team
    RECENT = "recent"
    OLD = "old"
    EXPIRING = "expiring"
    NEW = "new"  # not in the compare file
    VERIFICATION_NEEDED = "verification-needed"
    VERIFICATION_DONE = "verification-done"
    FREEZER = "freezer"
    GONE = "gone"  # in the compare file, but not found anymore


class Link(NamedTuple):
    text: str
    url: str = ""  # plain text if empty


@dataclass
class ReportItem:
    """One task of any source, as a report row."""

    source: str
    key: str  # unique per source, as persisted by BugPersistor
    label: str
    url: str
    title: str
    context: list[Link] = field(default_factory=list)  # package, repo, category, versions
    refs: list[Link] = field(default_factory=list)  # after the title, e.g. bugs blocking a migration
    status: str = ""
    importance: str = ""
    assignees: list[Link] = field(default_factory=list)
    updated: datetime | None = None
    releases: list[tuple[str, str]] = field(default_factory=list)  # (series letter, state)
    further: list[str] = field(default_factory=list)  # other tasks of the same bug
    flags: set[Flag] = field(default_factory=set)

    @property
    def context_text(self) -> str:
        return " ".join(link.text for link in self.context)


class TriageResult(ABC):
    """Per-source triage outcome; ``error`` is set when the fetch itself failed.

    A string error is a ready-to-display one-line message for known failures;
    an exception is printed with a full traceback.
    """

    error: Exception | str | None = None

    @abstractmethod
    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def record(self, persistor: BugPersistor) -> None:
        raise NotImplementedError

    @abstractmethod
    async def to_dict(self) -> dict[str, Any]:
        """Structured result of builtins and datetimes; ``json.dumps(..., default=json_default)`` it."""
        raise NotImplementedError

    async def report_items(self, persistor: BugPersistor | None) -> list[ReportItem]:
        """Rows for the html report; *persistor* flags new and adds gone items."""
        return []


class FailedTriageResult(TriageResult):
    """TriageResult stand-in for a source whose fetch raised an exception."""

    def __init__(self, error: Exception | str) -> None:
        self.error = error

    async def print_section(self, cfg: OutputConfig) -> None:
        """Nothing to render; the traceback is reported to stderr."""

    async def record(self, persistor: BugPersistor) -> None:
        """Nothing to persist for a failed fetch."""

    async def to_dict(self) -> dict[str, Any]:
        if isinstance(self.error, str):
            return {"error": self.error}
        return {"error": "".join(traceback.format_exception_only(self.error)).strip()}


def json_default(obj: object) -> str:
    """``json.dumps`` hook for the datetimes in ``TriageResult.to_dict`` output."""
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


@lru_cache(maxsize=256)
def hyperlink(
    url: str, text: str, fmt: OutputFormat = OutputFormat.TERMINAL, pad_right: int | None = None
) -> str:
    """Format text as a hyperlink for the given output format.
    pad_right: pad the resulting string, but the clickable surface remains just text.

    Terminal: ANSI OSC8 escape sequence (only when stdout is a TTY).
    Markdown: [text](url)
    """
    match fmt:
        case OutputFormat.MARKDOWN:
            return f"[{text}]({url})"
        case OutputFormat.TERMINAL:
            if os.isatty(sys.stdout.fileno()):
                osc8 = "\x1b]8"
                st = "\x1b\\"
                padding = ""
                if pad_right is not None:
                    padding_len = max(0, pad_right - len(text))
                    padding = " " * padding_len
                return f"{osc8};;{url}{st}{text}{osc8};;{st}{padding}"
            return text
        case _:
            raise NotImplementedError


def truncate_string(text: str, length: int = 20, pad: bool = False) -> str:
    s = str(text)
    if len(s) > length:
        return s[: length - 1] + "\u2026"  # triple dot ellipsis
    if pad:
        return s.ljust(length)
    return s
