"""Live terminal progress and optional thinking stream for AI triage runs."""

from __future__ import annotations

import sys
from typing import IO, Any, Self

from ..analysis import ActionKind, AnalysisState, ItemAnalysis, WorkItem
from ..spinner import Spinner
from .phases import PhaseOutcome
from .provider import SessionEvent, SessionEventKind

_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _format_tokens(count: int) -> str:
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _format_tool_arg(args: dict[str, Any]) -> str:
    for key in ("package", "path", "bug_number", "query", "url", "ref", "pattern"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[:40] + "…" if len(text) > 40 else text
    return ""


def format_item_summary(index: int, total: int, analysis: ItemAnalysis) -> str:
    """One-line persistent terminal summary for a completed item."""
    prefix = f"[{index}/{total}] "
    ident = f"LP #{analysis.key}" if analysis.source == "launchpad" else f"{analysis.source}:{analysis.key}"
    pkg = f" ({analysis.package})" if analysis.package else ""
    if analysis.state is AnalysisState.failed:
        return f"{prefix}{ident}{pkg}: failed — {analysis.error}"
    if analysis.state is AnalysisState.denied:
        return f"{prefix}{ident}{pkg}: private (not sent)"
    if analysis.state is AnalysisState.skipped:
        return f"{prefix}{ident}{pkg}: skipped ({analysis.note or analysis.route})"

    parts: list[str] = []
    if analysis.duplicate_of:
        parts.append(f"Duplicate #{analysis.duplicate_of}")
    elif analysis.status and analysis.status != "no-change":
        parts.append(analysis.status)
    else:
        parts.append("no-change")
    if analysis.has_fix:
        fix_states = [c.state for c in analysis.fix_checks.values()]
        if fix_states and all(s in ("clean", "offset", "already applied") for s in fix_states):
            parts.append("fix [clean]")
        else:
            parts.append("fix")
    elif any(a.kind is ActionKind.prepare_fix for a in analysis.actions):
        parts.append("root cause")
    elif all(a.kind is ActionKind.none for a in analysis.actions):
        parts = ["no action"]
    if analysis.tags:
        parts.append(", ".join(analysis.tags))

    sessions = sum(p.sessions for p in analysis.phases)
    tokens = sum(p.input_tokens + p.output_tokens for p in analysis.phases)
    meta = f" ({sessions}p, {_format_tokens(tokens)} tok)" if sessions else ""
    return f"{prefix}{ident}{pkg}: {' · '.join(parts)}{meta}"


class AIProgress:
    """Drives the spinner and persistent terminal lines for an AI analysis run.

    Each item updates the spinner line while active (showing current phase and
    active tool). When the item finishes, its summary line is printed and kept
    with a trailing newline so the next item starts on a fresh line.
    """

    def __init__(
        self,
        total: int,
        *,
        verbose: bool = False,
        show_thinking: bool = False,
        stream: IO[str] | None = None,
        is_tty: bool | None = None,
    ) -> None:
        self._total = total
        self._verbose = verbose
        self._show_thinking = show_thinking
        self._stream = stream if stream is not None else sys.stderr
        self._is_tty = is_tty if is_tty is not None else self._stream.isatty()

        def write_raw(text: str) -> None:
            self._stream.write(text)
            self._stream.flush()

        self._spinner = Spinner(
            set(),
            status=f"Preparing to analyze {total} {'item' if total == 1 else 'items'}…",
            out=write_raw if self._is_tty else None,
        )
        if not self._is_tty:
            self._spinner._enabled = False
        self._label = ""
        self._count = ""
        self._phase = ""
        self._in_thinking = False
        self._thinking_at_bol = True

    async def __aenter__(self) -> Self:
        await self._spinner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._finish_thinking()
        await self._spinner.__aexit__(*exc)

    def _emit_line(self, text: str) -> None:
        self._finish_thinking()
        if self._is_tty:
            self._spinner.suspend()
            self._spinner.clear()
        self._stream.write(text + "\n")
        self._stream.flush()
        if self._is_tty:
            self._spinner.resume()

    def _finish_thinking(self) -> None:
        if not self._in_thinking:
            return
        if not self._thinking_at_bol:
            self._stream.write("\n")
        if self._is_tty:
            self._stream.write(_RESET)
        self._stream.flush()
        self._in_thinking = False
        self._thinking_at_bol = True
        if self._is_tty:
            self._spinner.resume()

    def _write_thinking(self, text: str) -> None:
        if not self._show_thinking or not text:
            return
        if not self._in_thinking:
            if self._is_tty:
                self._spinner.suspend()
                self._spinner.clear()
                self._stream.write(_DIM)
            self._in_thinking = True
            self._thinking_at_bol = True
        for chunk in text.splitlines(keepends=True):
            if self._thinking_at_bol:
                self._stream.write("    ")
            self._stream.write(chunk)
            self._thinking_at_bol = chunk.endswith("\n")
        self._stream.flush()

    def _refresh_status(self, detail: str = "") -> None:
        parts = [f"[{self._count}] {self._label}"]
        if self._phase:
            parts.append(self._phase)
        if detail:
            parts.append(detail)
        self._spinner.set_status(" · ".join(parts) + "…")

    def on_item_start(self, index: int, total: int, item: WorkItem) -> None:
        self._finish_thinking()
        ident = f"LP #{item.key}" if item.source == "launchpad" else f"{item.source}:{item.key}"
        self._label = f"{ident} ({item.package})" if item.package else ident
        self._count = f"{index}/{total}"
        self._phase = ""
        self._refresh_status()
        if self._verbose or self._show_thinking:
            self._emit_line(f"[{self._count}] {self._label}")

    def on_phase_start(self, phase: str) -> None:
        self._finish_thinking()
        self._phase = phase
        self._refresh_status()
        if self._show_thinking:
            self._emit_line(f"  · {phase}")

    def on_event(self, event: SessionEvent) -> None:
        match event.kind:
            case SessionEventKind.reasoning_delta:
                self._write_thinking(event.text)
            case SessionEventKind.reasoning:
                self._write_thinking(event.text.rstrip() + "\n")
                self._finish_thinking()
            case SessionEventKind.intent:
                self._refresh_status(event.text)
                if self._show_thinking:
                    self._emit_line(f"    [{event.text}]")
            case SessionEventKind.tool_start:
                arg = _format_tool_arg(event.tool_args)
                detail = f"{event.tool_name}({arg})" if arg else event.tool_name
                self._finish_thinking()
                self._refresh_status(detail)
                if self._verbose or self._show_thinking:
                    self._emit_line(f"    → {detail}")
            case SessionEventKind.nudge:
                self._finish_thinking()
                self._refresh_status("nudging")

    def on_phase_done(self, phase: PhaseOutcome) -> None:
        self._finish_thinking()
        if not self._verbose:
            return
        summary = " ".join(phase.summary.split())
        if len(summary) > 100:
            summary = summary[:99] + "…"
        detail = f": {summary}" if summary else ""
        self._emit_line(f"  · {phase.phase} ({phase.state.value}){detail}")

    def on_item_done(self, index: int, total: int, analysis: ItemAnalysis) -> None:
        self._emit_line(format_item_summary(index, total, analysis))
