"""Tests for Launchpad triage table alignment."""

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from startriage.sources.launchpad.models import RenderContext, Task

# Matches OSC8 hyperlink wrappers and SGR colour codes so we can measure the
# *visible* width of a rendered cell regardless of whether stdout is a TTY.
_ANSI_RE = re.compile(r"\x1b\][^\x1b]*\x1b\\|\x1b\[[0-9;]*m")


def _visible(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _bug_cell(line: str) -> str:
    """Visible content of the first (bug-reference) table column."""
    return _visible(line).split(" | ")[0]


class _FakeLPTask:
    """Minimal stand-in for a launchpadlib bug task, enough to render a row."""

    def __init__(self, number: str, src: str = "foo", status: str = "New") -> None:
        self.title = f"Bug #{number} in {src} (Ubuntu): example title"
        self.status = status
        self.importance = "Undecided"
        self.assignee_link = None
        self.target = SimpleNamespace(resource_type_link="x")
        self.bug = SimpleNamespace(
            tags=[],
            date_last_updated=datetime(2026, 6, 1, tzinfo=timezone.utc),
            bug_tasks=[self],
        )
        self._self_link = f"https://api.launchpad.net/devel/ubuntu/+source/{src}/+bug/{number}"

    def __str__(self) -> str:
        return self._self_link


def _make_task(number: str) -> Task:
    return Task(_FakeLPTask(number), subscribed=False, last_activity_ours=True)


@pytest.mark.parametrize("number", ["815", "1234567", "20000001"])
@pytest.mark.parametrize("extended", [False, True])
def test_header_and_row_bug_column_aligned(number: str, extended: bool) -> None:
    ctx = RenderContext()
    bugid_len = len(number)

    header = Task.get_table_header(bugid_len, extended=extended)
    row = _make_task(number).get_table_row(ctx, bugid_len, shortlinks=True, extended=extended)

    expected_width = Task.bug_col_width(bugid_len)
    assert len(_bug_cell(header)) == expected_width
    assert len(_bug_cell(row)) == expected_width
    assert len(_bug_cell(header)) == len(_bug_cell(row))
