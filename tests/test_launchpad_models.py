"""Tests for the Launchpad Task model, focused on the AI agent payload."""

from __future__ import annotations

from datetime import datetime, timezone

from startriage.sources.launchpad.models import (
    DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK,
    Task,
)


class _FakeMessage:
    def __init__(self, owner_link, date_created, content):
        self.owner_link = owner_link
        self.date_created = date_created
        self.content = content


class _FakeAttachment:
    def __init__(self, title, type_):
        self.title = title
        self.type = type_


class _FakeDuplicate:
    def __init__(self, id_):
        self.id = id_


class _FakeTarget:
    resource_type_link = DISTRIBUTION_SOURCE_PACKAGE_RESOURCE_TYPE_LINK


class _FakeLPTask:
    """Minimal stand-in for a launchpadlib bug_task entry."""

    def __init__(self, api_url, *, status, importance, target_name, bug=None):
        self._api_url = api_url
        self.status = status
        self.importance = importance
        self.bug_target_name = target_name
        self.title = "Bug #123 in pkg (Ubuntu): boom on start"
        self.assignee_link = None
        self.target = _FakeTarget()
        self.bug = bug

    def __str__(self):
        return self._api_url


class _FakeBug:
    def __init__(self, *, bug_tasks, messages, attachments, duplicate_of):
        self.description = "It crashes immediately."
        self.tags = ["amd64", "regression-release"]
        self.date_last_updated = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self.heat = 42
        self.messages = messages
        self.attachments = attachments
        self.duplicate_of = duplicate_of
        self.bug_tasks = bug_tasks


def _build_task(duplicate_of=None) -> Task:
    devel_url = "https://api.launchpad.net/devel/ubuntu/+source/pkg/+bug/123"
    jammy_url = "https://api.launchpad.net/devel/ubuntu/jammy/+source/pkg/+bug/123"

    messages = [
        _FakeMessage(
            "https://api.launchpad.net/devel/~reporter",
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            "original report body",
        ),
        _FakeMessage(
            "https://api.launchpad.net/devel/~helper",
            datetime(2026, 5, 2, tzinfo=timezone.utc),
            "have you tried turning it off and on again?",
        ),
    ]
    attachments = [
        _FakeAttachment("crash.txt", "Unspecified"),
        _FakeAttachment("fix.patch", "Patch"),
    ]

    devel_task = _FakeLPTask(devel_url, status="New", importance="Undecided", target_name="pkg (Ubuntu)")
    jammy_task = _FakeLPTask(
        jammy_url, status="Confirmed", importance="High", target_name="pkg (Ubuntu Jammy)"
    )

    bug = _FakeBug(
        bug_tasks=[devel_task, jammy_task],
        messages=messages,
        attachments=attachments,
        duplicate_of=duplicate_of,
    )
    devel_task.bug = bug
    jammy_task.bug = bug

    return Task(devel_task, subscribed=False, last_activity_ours=False)


def test_to_agent_payload_core_fields():
    payload = _build_task().to_agent_payload()
    assert payload["number"] == "123"
    assert payload["url"] == "https://bugs.launchpad.net/ubuntu/+bug/123"
    assert payload["description"] == "It crashes immediately."
    assert payload["status"] == "New"
    assert payload["importance"] == "Undecided"
    assert payload["tags"] == ["amd64", "regression-release"]
    assert payload["heat"] == 42
    assert payload["duplicate_of"] is None


def test_to_agent_payload_comments_skip_original_report():
    payload = _build_task().to_agent_payload()
    # The first message is the original report (covered by description).
    assert len(payload["comments"]) == 1
    comment = payload["comments"][0]
    assert comment["author"] == "helper"
    assert comment["text"] == "have you tried turning it off and on again?"
    assert comment["date"] == "2026-05-02T00:00:00+00:00"


def test_to_agent_payload_attachments():
    payload = _build_task().to_agent_payload()
    assert payload["attachments"] == [
        {"title": "crash.txt", "type": "Unspecified", "is_patch": False},
        {"title": "fix.patch", "type": "Patch", "is_patch": True},
    ]


def test_to_agent_payload_affected_targets():
    payload = _build_task().to_agent_payload()
    affected = payload["affected"]
    assert len(affected) == 2

    devel = affected[0]
    assert devel["target"] == "pkg (Ubuntu)"
    assert devel["package"] == "pkg"
    assert devel["distro"] == "ubuntu"
    assert devel["series"] is None
    assert devel["status"] == "New"

    jammy = affected[1]
    assert jammy["package"] == "pkg"
    assert jammy["series"] == "jammy"
    assert jammy["status"] == "Confirmed"
    assert jammy["importance"] == "High"


def test_to_agent_payload_duplicate_of():
    payload = _build_task(duplicate_of=_FakeDuplicate(999)).to_agent_payload()
    assert payload["duplicate_of"] == "999"
