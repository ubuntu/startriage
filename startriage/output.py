"""Shared output helpers for startriage."""

from __future__ import annotations

import os
import sys
from enum import StrEnum
from functools import lru_cache


class OutputFormat(StrEnum):
    TERMINAL = "terminal"
    MARKDOWN = "markdown"


@lru_cache(maxsize=256)
def hyperlink(url: str, text: str, fmt: OutputFormat = OutputFormat.TERMINAL) -> str:
    """Format text as a hyperlink for the given output format.

    Terminal: ANSI OSC8 escape sequence (only when stdout is a TTY).
    Markdown: [text](url)
    """
    if fmt == OutputFormat.MARKDOWN:
        return f"[{text}]({url})"
    if os.isatty(sys.stdout.fileno()):
        osc8 = "\x1b]8"
        st = "\x1b\\"
        return f"{osc8};;{url}{st}{text}{osc8};;{st}"
    return text


def print_section_header(title: str, level: int = 1) -> None:
    """Print a Markdown-style section header."""
    print(f"\n{'#' * level} {title}\n")
