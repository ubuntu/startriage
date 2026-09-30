"""Tests for the library entry points and structured (to_dict/JSON) results."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from startriage.config import load_config
from startriage.enums import FetchMode
from startriage.output import FailedTriageResult, OutputConfig, OutputFormat, TriageResult, json_default
from startriage.report import triage
from startriage.source import TriageSource
from startriage.sources.discourse.finder import DiscourseFinder
from startriage.sources.discourse.models import DiscoursePost, DiscourseTopic
from startriage.sources.discourse.triage import PostStatus, _topic_activity
from startriage.sources.github.models import Issue, RepoResult
from startriage.sources.github.triage import GithubTriage
from startriage.sources.launchpad.finder import LaunchpadAuthError, _NoAuthorization
from startriage.sources.proposed.models import MigrationExcuse, ProposedMigrationData
from startriage.sources.proposed.triage import ProposedMigrationTriage
from startriage.triage import build_filter, fetch

START = datetime(2026, 9, 28, tzinfo=timezone.utc)
END = datetime(2026, 9, 28, 23, 59, 59, tzinfo=timezone.utc)


def _proposed() -> ProposedMigrationTriage:
    excuse = MigrationExcuse("pkg", "1.0", "1.1", START, reasons=["autopkgtest"], bugs=[123])
    return ProposedMigrationTriage(data=ProposedMigrationData(START, [excuse]), teams=["ubuntu-server"])


def _source(name: str, result: TriageResult | Exception) -> TriageSource:
    async def find(config, opts, mode):
        if isinstance(result, Exception):
            raise result
        return result

    return TriageSource(name=name, find=find)


@pytest.fixture
def config(tmp_path):
    return load_config(tmp_path / "nonexistent.toml")


def test_build_filter(config):
    opts = build_filter(config, interval="2026-09-28", sources="proposed,github")
    assert opts.team == "ubuntu-server"
    assert (opts.start.date(), opts.end.date()) == (START.date(), START.date())
    assert {s.name for s in opts.sources} == {"proposed", "github"}


def test_build_filter_exclusive(config):
    with pytest.raises(ValueError):
        build_filter(config, interval="2026-09-28", triage_day="monday")


@pytest.mark.asyncio
async def test_fetch_captures_errors(config):
    ok = _proposed()
    opts = build_filter(config, interval="2026-09-28")
    opts = type(opts)(
        **{**opts.__dict__, "sources": frozenset({_source("ok", ok), _source("bad", KeyError("x"))})}
    )

    seen = []

    async def on_result(source, result):
        seen.append(source)

    results = await fetch(config, opts, FetchMode.triage, on_result=on_result)

    assert sorted(seen) == ["bad", "ok"]
    assert results["ok"] is ok
    assert isinstance(results["bad"], FailedTriageResult)
    assert await results["bad"].to_dict() == {"error": "KeyError: 'x'"}


@pytest.mark.asyncio
async def test_render_json(config):
    opts = build_filter(config, interval="2026-09-28")
    opts = type(opts)(**{**opts.__dict__, "sources": frozenset({_source("proposed", _proposed())})})
    out = io.StringIO()

    await triage(config, opts, OutputConfig(fmt=OutputFormat.JSON, out=out))

    data = json.loads(out.getvalue())
    excuse = data["proposed"]["excuses"][0]
    assert excuse["package"] == "pkg"
    assert excuse["in_proposed_since"] == START.isoformat()
    assert excuse["bugs"] == [123]


@pytest.mark.asyncio
async def test_github_to_dict():
    issue = Issue(
        1, "title", "https://github.com/o/r/issues/1", "https://github.com/o/r", START, START, "open"
    )
    triage = GithubTriage(start=START.date(), end=END.date(), results=[RepoResult("o/r", issues=[issue])])

    data = json.loads(json.dumps(await triage.to_dict(), default=json_default))

    assert data["repos"][0]["issues"][0]["html_url"] == issue.html_url
    assert data["start"] == "2026-09-28"


def test_topic_activity_tree():
    topic = DiscourseTopic({"id": 7, "title": "T", "category_id": 1})
    before = "2026-09-01T00:00:00Z"
    during = "2026-09-28T12:00:00Z"
    topic.add_post(
        DiscoursePost({"id": 1, "post_number": 1, "raw": "main", "created_at": before, "updated_at": before})
    )
    topic.add_post(
        DiscoursePost(
            {
                "id": 2,
                "post_number": 2,
                "raw": "reply",
                "username": "u",
                "created_at": during,
                "updated_at": during,
            }
        )
    )
    finder = DiscourseFinder("https://example.com")

    assert _topic_activity(finder, topic, START, START.replace(day=27), is_triage=False) is None

    activity = _topic_activity(finder, topic, START, END, is_triage=False)
    assert activity is not None
    data = activity.to_dict()
    assert data["url"] == "https://example.com/t/7"
    assert data["status"] == PostStatus.UNCHANGED
    assert [t["content"] for t in data["threads"]] == ["reply"]
    assert data["threads"][0]["status"] == "new"


def test_launchpad_noninteractive_auth():
    engine = _NoAuthorization("production", consumer_name="startriage")
    with pytest.raises(LaunchpadAuthError, match="startriage login launchpad"):
        engine.make_end_user_authorize_token(None, "token")
