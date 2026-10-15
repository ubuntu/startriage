"""Tests for the host precheck (routes and gathered facts) — pure, offline."""

from __future__ import annotations

from datetime import UTC, datetime

from startriage.ai.precheck import Route, build_precheck, parse_apport, process_hints

TEAM = frozenset({"ubuntu-server", "alice", "bob"})
WINDOW = (datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 5, 23, 59, tzinfo=UTC))
IN = "2026-10-05T10:00:00+00:00"
BEFORE = "2026-09-01T10:00:00+00:00"


def _payload(**overrides) -> dict:
    payload = {
        "number": "100",
        "package": "pkg",
        "title": 'Bug #100 in pkg (Ubuntu): "crash on start"',
        "short_title": "crash on start",
        "description": "It crashes.\n\nProblemType: Bug\nDistroRelease: Ubuntu 24.04\nPackage: pkg 1.0-1",
        "reporter": "carol",
        "date_created": BEFORE,
        "status": "New",
        "importance": "Undecided",
        "tags": [],
        "assignees": [],
        "affected": [{"target": "pkg (Ubuntu)", "status": "New", "series": None, "package": "pkg"}],
        "attachments": [],
        "comments": [],
        "private": False,
        "information_type": "Public",
        "expiring": False,
        "activity": [],
    }
    payload.update(overrides)
    return payload


def _route(payload, window=WINDOW):
    return build_precheck(payload, team=TEAM, window=window)


def test_private_is_denied():
    pre = _route(_payload(private=True, information_type="Private Security"))
    assert pre.route is Route.denied
    assert "Private Security" in pre.reasons[0]


def test_only_team_activity_without_comment_is_skipped():
    activity = [{"date": IN, "person": "alice", "what": "pkg (Ubuntu): status"}]
    pre = _route(_payload(activity=activity))
    assert pre.route is Route.skip
    assert "alice" in pre.reasons[0]


def test_team_comment_in_window_is_not_skipped():
    comments = [{"author": "alice", "date": IN, "text": "Thanks"}]
    activity = [{"date": IN, "person": "alice", "what": "pkg (Ubuntu): status"}]
    assert _route(_payload(comments=comments, activity=activity)).route is not Route.skip


def test_outside_activity_in_window_is_not_skipped():
    activity = [
        {"date": IN, "person": "alice", "what": "tags"},
        {"date": IN, "person": "dave", "what": "tags"},
    ]
    assert _route(_payload(activity=activity)).route is Route.new


def test_no_window_means_no_skip():
    activity = [{"date": IN, "person": "alice", "what": "tags"}]
    assert _route(_payload(activity=activity), window=None).route is Route.new


def test_update_routes():
    assert _route(_payload(status="Triaged")).route is Route.update
    assert _route(_payload(assignees=["bob"])).route is Route.update
    assert _route(_payload(tags=["verification-needed-noble"])).route is Route.update
    assert _route(_payload(expiring=True)).route is Route.update
    affected = [{"target": "pkg (Ubuntu Noble)", "status": "Fix Committed", "series": "noble"}]
    assert _route(_payload(affected=affected)).route is Route.update


def test_team_comment_before_window_is_update_but_in_window_is_not():
    before = [{"author": "bob", "date": BEFORE, "text": "Looking"}]
    pre = _route(_payload(comments=before))
    assert pre.route is Route.update
    assert "bob commented on 2026-09-01" in pre.reasons

    within = [{"author": "dave", "date": IN, "text": "me too"}, {"author": "bob", "date": IN, "text": "ok"}]
    assert _route(_payload(comments=within)).route is Route.new


def test_new_comments_and_last_team_comment():
    comments = [
        {"author": "bob", "date": BEFORE, "text": "Please attach logs"},
        {"author": "carol", "date": IN, "text": "Logs attached"},
    ]
    pre = _route(_payload(comments=comments))
    assert pre.last_team_comment is not None
    assert pre.last_team_comment.text == "Please attach logs"
    assert [c.text for c in pre.new_comments] == ["Logs attached"]
    # Without a window, new comments are the ones after the team's last comment.
    pre = _route(_payload(comments=comments), window=None)
    assert [c.text for c in pre.new_comments] == ["Logs attached"]


def test_team_filed_and_gathered_facts():
    attachments = [
        {"index": 0, "title": "fix.debdiff", "type": "Unspecified", "is_patch": False},
        {"index": 1, "title": "log.txt", "type": "Unspecified", "is_patch": False},
        {"index": 2, "title": "upstream", "type": "Patch", "is_patch": True},
    ]
    affected = [{"target": "pkg (Ubuntu Noble)", "status": "New", "series": "noble", "package": "pkg"}]
    pre = _route(_payload(reporter="alice", date_created=IN, attachments=attachments, affected=affected))
    assert pre.team_filed
    assert pre.patches == ["fix.debdiff", "upstream"]
    assert pre.affected_series == ["noble"]
    assert pre.apport == {"ProblemType": "Bug", "DistroRelease": "Ubuntu 24.04", "Package": "pkg 1.0-1"}


def test_parse_apport_ignores_other_lines():
    assert parse_apport("Error: Package: x\nArchitecture: amd64\nArchitecture: i386") == {
        "Architecture": "amd64"
    }


def test_process_hints():
    assert process_hints("[MIR] foo", []) == ["MIR"]
    assert process_hints("Please merge foo 1.2-1 from Debian unstable", []) == ["merge/sync"]
    assert process_hints("FFe: new upstream release", ["needs-merge"]) == ["FFe", "tag needs-merge"]
    assert process_hints("foo FTBFS with gcc 15", []) == ["FTBFS"]
    assert process_hints("autopkgtest failure on arm64", []) == ["autopkgtest"]
    assert process_hints("crash on start", []) == []
