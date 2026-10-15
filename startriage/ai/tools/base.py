"""Building blocks shared by the agent's host tools.

A :class:`ToolDef` is a catalogue entry (what the user enables in
``[ai.tools]``); binding it to a :class:`ToolContext` gives a :class:`ToolSpec`,
which is what a provider exposes to the model for one session.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from launchpadlib.launchpad import Launchpad

    from .http import ReadOnlyHTTP

logger = logging.getLogger(__name__)

# Upper bound for what a single tool call returns to the model.
MAX_TOOL_OUTPUT = 20_000
# Lines returned per page when reading a long text (attachment, web page).
PAGE_LINES = 300
MAX_MATCHES = 200


class ToolError(Exception):
    """A tool failure reported back to the model, which may retry."""


class Risk(StrEnum):
    read = "read"  # reads public data, no side effects
    exec = "exec"  # runs programs on the host


Handler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolContext:
    """What tools may use: the user's Launchpad session, read-only HTTP, the run's scratch dir."""

    lp: Launchpad | None = None
    http: ReadOnlyHTTP | None = None
    workdir: Path | None = None


@dataclass(frozen=True)
class ToolSpec:
    """A tool as exposed to the model for one session."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    # A successful call ends the model's turn (used by submit tools).
    terminal: bool = False


@dataclass(frozen=True)
class ToolDef:
    """A catalogue entry: a tool the user can enable or disable."""

    name: str
    group: str
    risk: Risk
    default: bool
    summary: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]

    def bind(self, ctx: ToolContext) -> ToolSpec:
        async def handler(args: dict[str, Any]) -> str:
            return await self.run(ctx, args)

        return ToolSpec(self.name, self.description, self.parameters, handler)


async def call_tool(tool: ToolSpec, args: dict[str, Any]) -> tuple[str, bool]:
    """Run ``tool`` and return ``(text for the model, succeeded)``; never raises."""
    try:
        text = await tool.handler(args)
    except ToolError as exc:
        logger.debug("Tool %s refused: %s", tool.name, exc)
        return str(exc), False
    except Exception as exc:
        logger.warning("Tool %s failed", tool.name, exc_info=True)
        return f"{tool.name} failed: {type(exc).__name__}: {exc}", False
    if len(text) > MAX_TOOL_OUTPUT:
        omitted = len(text) - MAX_TOOL_OUTPUT
        logger.info("Tool %s output truncated: %d characters over the limit", tool.name, omitted)
        text = f"{text[:MAX_TOOL_OUTPUT]}\n[truncated, {omitted} more characters; narrow the request]"
    logger.debug("Tool %s returned %d characters", tool.name, len(text))
    return text, True


def page_text(text: str, *, pattern: str | None = None, offset: int = 0) -> str:
    """Return the lines of ``text`` matching ``pattern``, or a page of it from ``offset``."""
    lines = text.splitlines()
    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc
        matches = [f"{n}: {line}" for n, line in enumerate(lines, start=1) if regex.search(line)]
        if not matches:
            return f"No line of {len(lines)} matches {pattern!r}."
        more = len(matches) - MAX_MATCHES
        note = f"\n[{more} more matches; use a narrower pattern]" if more > 0 else ""
        return "\n".join(matches[:MAX_MATCHES]) + note
    offset = max(offset, 0)
    page = lines[offset : offset + PAGE_LINES]
    end = offset + len(page)
    header = f"[lines {offset + 1}-{end} of {len(lines)}"
    header += f"; continue with offset={end}]" if end < len(lines) else "]"
    return "\n".join([header, *page])


def need_http(ctx: ToolContext) -> ReadOnlyHTTP:
    if ctx.http is None:
        raise ToolError("HTTP is not available in this run")
    return ctx.http


def str_arg(args: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = str(args.get(key) or "").strip()
    if required and not value:
        raise ToolError(f"{key} is required")
    return value


def int_arg(args: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(args.get(key) or default)
    except (TypeError, ValueError):
        raise ToolError(f"{key} must be an integer") from None


def parse_bug_arg(args: dict[str, Any], key: str = "bug") -> int:
    """Return the Launchpad bug number in ``args[key]`` (accepts ``123`` or ``"#123"``)."""
    value = str(args.get(key, "")).strip().lstrip("#")
    if not value.isdigit():
        raise ToolError(f"{key} must be a Launchpad bug number, got {args.get(key)!r}")
    return int(value)
