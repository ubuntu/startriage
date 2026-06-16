"""Tests for the async terminal spinner."""

from __future__ import annotations

import asyncio

import pytest

from startriage.spinner import Spinner


@pytest.mark.asyncio
async def test_spinner_shows_status_message():
    frames: list[str] = []
    spinner = Spinner(set(), status="Starting…", out=frames.append, interval=1 / 1000)
    async with spinner:
        await asyncio.sleep(0.02)
        spinner.set_status("Triaging LP #123 (1/2)…")
        await asyncio.sleep(0.02)

    rendered = "".join(frames)
    assert "Starting…" in rendered
    assert "Triaging LP #123 (1/2)…" in rendered


@pytest.mark.asyncio
async def test_spinner_status_overrides_pending_set():
    frames: list[str] = []
    spinner = Spinner({"launchpad"}, status="Working…", out=frames.append, interval=1 / 1000)
    async with spinner:
        await asyncio.sleep(0.02)

    rendered = "".join(frames)
    assert "Working…" in rendered
    assert "launchpad" not in rendered
