"""Tests for Launchpad last-activity attribution."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast

import pytest
from launchpadlib.launchpad import Launchpad

from startriage.config import TeamConfig
from startriage.source import FetchMode, TaskFilterOptions
from startriage.sources.launchpad.finder import _fetch_last_activity, fetch_bugs

D0, D1, D2 = (datetime(2026, 10, day, tzinfo=timezone.utc) for day in (1, 2, 3))
FIXED = "This bug was fixed in the package cacti - 1.2.31+ds1-3ubuntu2\n\n---------------\ncacti (...) ..."
EXPIRED = "[Expired for cacti (Ubuntu) because there has been no activity for 60 days.]"
CONFIRMED = "Status changed to 'Confirmed' because the bug affects multiple users."


def _link(name):
    return f"https://api.launchpad.net/devel/~{name}"


def _msg(dt, by, content=""):
    return SimpleNamespace(date_created=dt, owner_link=_link(by), content=content)


def _act(dt, by, whatchanged="cacti (Ubuntu): status", newvalue="In Progress"):
    return SimpleNamespace(datechanged=dt, person_link=_link(by), whatchanged=whatchanged, newvalue=newvalue)


def _task(messages, activity):
    return SimpleNamespace(bug=SimpleNamespace(messages=messages, activity=activity))


@pytest.mark.parametrize(
    "messages, activity, expected",
    [
        ([_msg(D1, "member")], [_act(D2, "stranger")], (D2, False)),
        ([_msg(D1, "stranger")], [_act(D2, "member")], (D2, True)),
        # janitor closing after an upload: the member's earlier change is the last relevant one
        (
            [_msg(D0, "stranger"), _msg(D2, "janitor", FIXED)],
            [_act(D1, "member"), _act(D2, "janitor", newvalue="Fix Released")],
            (D1, True),
        ),
        ([_msg(D2, "janitor", FIXED)], [_act(D2, "janitor", newvalue="Fix Released")], (None, False)),
        ([_msg(D2, "janitor", EXPIRED)], [_act(D2, "janitor", newvalue="Expired")], (None, False)),
        # other janitor events still count, e.g. confirming a bug that affects multiple users
        ([_msg(D2, "janitor", CONFIRMED)], [_act(D2, "janitor", newvalue="Confirmed")], (D2, False)),
        ([_msg(D2, "janitor", "something else")], [], (D2, False)),
        ([], [_act(D2, "janitor", newvalue="Triaged")], (D2, False)),
        (
            [_msg(D1, "stranger")],
            [_act(D2, "janitor", "merge proposal linked", "lp:~x/+merge/1")],
            (D1, False),
        ),
        (
            [_msg(D1, "stranger")],
            [_act(D2, "janitor", "merge proposal unlinked", "lp:~x/+merge/1")],
            (D1, False),
        ),
        ([], [_act(D2, "janitor", "merge proposal unlinked", "lp:~x/+merge/1")], (None, False)),
    ],
)
def test_last_activity(messages, activity, expected):
    assert _fetch_last_activity(_task(messages, activity), {_link("member")}) == expected


def test_triage_omits_bug_with_only_ignored_activity():
    bug_link = "https://api.launchpad.net/devel/bugs/2171094"
    lp_task = SimpleNamespace(
        bug_link=bug_link,
        target_link="https://api.launchpad.net/devel/ubuntu/+source/openssh",
        bug=SimpleNamespace(messages=[], activity=[_act(D2, "janitor", "merge proposal unlinked")]),
    )
    lp = SimpleNamespace(
        distributions={
            "Ubuntu": SimpleNamespace(series_collection=[], searchTasks=lambda **kwargs: [lp_task])
        },
        people={"ubuntu-server": SimpleNamespace(participants=[])},
    )
    task_filter = TaskFilterOptions(
        team="ubuntu-server",
        start=D1,
        end=D2,
        recent_since=D1,
        old_since=D0,
        sources=frozenset(),
        show_expiration=False,
    )

    result = fetch_bugs(
        cast(Launchpad, lp),
        TeamConfig(lp_team="ubuntu-server", lp_todo_tag="server-todo"),
        task_filter,
        FetchMode.triage,
        update_filter=None,
    )

    assert result.tasks == []
