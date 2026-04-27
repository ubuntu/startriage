"""Shared output helpers for startriage."""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import IO

from .savebugs import BugPersistor


class OutputFormat(StrEnum):
    TERMINAL = "terminal"
    MARKDOWN = "markdown"
    # TODO: OutputConfig should then provide something like an
    # out `dict` so we can properly nest items
    JSON = "json"


@dataclass
class OutputConfig:
    fmt: OutputFormat
    out: IO[str]
    open_in_browser: bool = False
    terminal_links: bool = True
    bug_persistor: BugPersistor | None = None
    markdown_path: Path | None = None


class TriageResult(ABC):
    @abstractmethod
    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def record(self, persistor: BugPersistor) -> None:
        raise NotImplementedError


@lru_cache(maxsize=256)
def hyperlink(url: str, text: str, fmt: OutputFormat = OutputFormat.TERMINAL) -> str:
    """Format text as a hyperlink for the given output format.

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
                return f"{osc8};;{url}{st}{text}{osc8};;{st}"
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
