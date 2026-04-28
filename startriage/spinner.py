"""Async terminal spinner that tracks named pending tasks."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from typing import Self


class Spinner:
    """Braille spinner that shows which named items are still pending.

    Usage::

        spinner = Spinner({"launchpad", "discourse", "github"})
        async with spinner:
            result = await some_task()
            spinner.done("launchpad")   # removes it from the displayed list
            spinner.clear()             # erase line before printing output
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(
        self,
        pending: set[str],
        *,
        out: Callable[[str], None] | None = None,
        interval: float = 1 / 10,  # 10 FPS
    ) -> None:
        self._pending = set(pending)
        if out:
            self._write = out
        else:
            self._write = lambda s: (sys.stderr.write(s), sys.stderr.flush())
        self._interval = interval
        self._draw = asyncio.Event()
        self._draw.set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def done(self, name: str) -> None:
        """Mark *name* as no longer pending."""
        self._pending.discard(name)

    def suspend(self) -> None:
        """Pause spinner writes (call before awaiting section output)."""
        self._draw.clear()

    def resume(self) -> None:
        """Resume spinner writes."""
        self._draw.set()

    def clear(self) -> None:
        """Erase the spinner line so subsequent stdout output starts cleanly."""
        self._write("\x1b[2K\r")

    async def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            await self._draw.wait()

            frame = self._FRAMES[i % len(self._FRAMES)]
            sources = sorted(self._pending)
            msg = f"Fetching: {', '.join(sources)}…" if sources else "Processing…"
            line = f"{frame} {msg}"
            self._write(f"\r{line}")

            i += 1
            try:
                await asyncio.wait_for(asyncio.shield(self._stop.wait()), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
        self.clear()

    async def __aenter__(self) -> Self:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
