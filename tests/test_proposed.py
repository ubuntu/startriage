"""Tests for startriage.sources.proposed.triage."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from startriage.config import load_config
from startriage.enums import FetchMode
from startriage.output import OutputConfig, OutputFormat
from startriage.source import TaskFilterOptions
from startriage.sources.proposed.models import ProposedMigrationData
from startriage.sources.proposed.triage import ProposedMigrationTriage, find


def _opts(end: datetime) -> TaskFilterOptions:
    now = datetime.now(timezone.utc)
    return TaskFilterOptions(
        team="ubuntu-server",
        start=end - timedelta(days=1),
        end=end,
        recent_since=now - timedelta(days=7),
        old_since=now - timedelta(days=30),
        sources=frozenset(),
    )


@pytest.mark.asyncio
async def test_skipped_for_old_interval(tmp_path):
    """An interval ending more than a week ago skips proposed (no network call)."""
    config = load_config(tmp_path / "nonexistent.toml")  # default ubuntu-server team
    old_end = datetime.now(timezone.utc) - timedelta(days=14)

    result = await find(config, _opts(old_end), FetchMode.triage)

    assert isinstance(result, ProposedMigrationTriage)
    assert result.skipped_reason is not None
    assert not result.had_updates


@pytest.mark.asyncio
async def test_fetched_for_recent_interval(tmp_path, monkeypatch):
    """A recent interval still fetches proposed migration data."""
    config = load_config(tmp_path / "nonexistent.toml")
    recent_end = datetime.now(timezone.utc)

    async def fake_fetch(teams, min_age, session):
        return ProposedMigrationData(generated_date=None, excuses=[])

    monkeypatch.setattr("startriage.sources.proposed.triage.fetch_proposed_migration", fake_fetch)

    result = await find(config, _opts(recent_end), FetchMode.triage)

    assert isinstance(result, ProposedMigrationTriage)
    assert result.skipped_reason is None


@pytest.mark.asyncio
async def test_skip_message_rendered():
    """print_section shows the skip reason instead of the package table."""
    triage = ProposedMigrationTriage(
        data=ProposedMigrationData(generated_date=None, excuses=[]),
        teams=["ubuntu-server"],
        skipped_reason="triage interval ends 2026-01-07, more than 7 days ago",
    )
    buf = io.StringIO()
    await triage.print_section(OutputConfig(fmt=OutputFormat.TERMINAL, out=buf))

    out = buf.getvalue()
    assert "Proposed Migration" in out
    assert "skipped" in out
