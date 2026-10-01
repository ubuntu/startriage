"""Tests for the html report."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from startriage.config import UpdateFilter
from startriage.htmlreport import render_page, todo_sections, write_report
from startriage.output import Flag, Link, ReportItem
from startriage.savebugs import BugPersistor, SaveConfig
from startriage.source import FetchMode
from startriage.sources.github.models import Issue, RepoResult
from startriage.sources.github.triage import GithubTriage, _apply_update_filter

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _item(key: str, *flags: Flag, assignees: tuple[str, ...] = ("me",)) -> ReportItem:
    return ReportItem(
        source="launchpad",
        key=key,
        label=f"LP: #{key}",
        url=f"https://bugs.launchpad.net/bugs/{key}",
        title=f"bug {key}",
        assignees=[Link(a, f"https://launchpad.net/~{a}") for a in assignees],
        flags=set(flags),
    )


def test_todo_sections_first_match():
    items = [
        _item("1", Flag.FREEZER, assignees=()),
        _item("2", Flag.OLD, assignees=()),
        _item("3", Flag.OLD, Flag.NEW),
        _item("4", Flag.NEW, Flag.VERIFICATION_NEEDED),
        _item("5", Flag.VERIFICATION_NEEDED, Flag.EXTERNAL),
        _item("6", Flag.EXTERNAL),
        _item("7"),
        _item("8", Flag.GONE, assignees=()),
    ]

    sections = {name: [i.key for i in section] for name, section in todo_sections(items)}

    assert sections == {
        "Bugs gone": ["8"],
        "No owner": ["2"],
        "No recent updates": ["3"],
        "Recently marked todo": ["4"],
        "Needs verification": ["5"],
        "External updates": ["6"],
        "Freezer": ["1"],
        "Other": ["7"],
    }


def test_render_page_escapes():
    item = _item("1")
    item.title = "<script>alert(1)</script>"
    item.refs = [Link("LP: #2", "https://bugs.launchpad.net/bugs/2")]

    page = render_page("T", "sub", [("Other", [item]), ("Empty", [])], NOW, filename="t-2026-10-01.md")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert '<a href="https://launchpad.net/~me">me</a>' in page
    assert '<a href="https://bugs.launchpad.net/bugs/2">LP: #2</a>' in page
    assert 'link.download = "t-2026-10-01.md"' in page
    assert 'id="launchpad-1"' in page
    # the markdown template is embedded as json, which must not close the script element
    assert "#### [LP: #1](https://bugs.launchpad.net/bugs/1) \\u2014 <script>alert(1)<\\/script>" in page
    assert page.count("</script>") == 1
    assert "Empty" not in page


@pytest.mark.asyncio
async def test_github_external():
    def issue(n: int, author: str, commenter: str | None, state: str = "open") -> Issue:
        url = f"https://github.com/o/r/issues/{n}"
        return Issue(
            n,
            "t",
            url,
            "https://github.com/o/r",
            NOW,
            NOW,
            state,
            author=author,
            latest_comment_author=commenter,
        )

    issues = [
        issue(1, "outsider", None),
        issue(2, "outsider", "member"),
        issue(3, "member", "outsider"),
        issue(4, "member", None),
        issue(5, "outsider", None, state="closed"),
    ]
    results = [RepoResult("o/r", issues=issues)]

    triage = GithubTriage(start=None, end=None, results=results, team_members={"member"})
    external = {i.key for i in await triage.report_items(None) if Flag.EXTERNAL in i.flags}
    assert external == {"o/r#1", "o/r#3"}

    unknown = GithubTriage(start=None, end=None, results=results)
    assert not any(Flag.EXTERNAL in i.flags for i in await unknown.report_items(None))

    issues.append(issue(6, "outsider", None))
    issues[-1].author = None  # deleted user
    for update_filter, expected in [
        (UpdateFilter.theirs, [1, 3, 5, 6]),
        (UpdateFilter.ours, [2, 4, 6]),
        (UpdateFilter.all, [1, 2, 3, 4, 5, 6]),
    ]:
        filtered = [RepoResult("o/r", issues=list(issues))]
        _apply_update_filter(filtered, update_filter, {"member"})
        assert [i.number for i in filtered[0].issues] == expected, update_filter


@pytest.mark.asyncio
async def test_github_gone_and_new(tmp_path):
    compare = tmp_path / "todo-2026-09-01.yaml"
    compare.write_text("version: 2\ngithub: ['o/r#1', 'o/r#2']\n")
    persistor = BugPersistor(SaveConfig(None, None, compare, no_save=True))

    issues = [
        Issue(n, f"t{n}", f"https://github.com/o/r/issues/{n}", "https://github.com/o/r", NOW, NOW, "open")
        for n in (1, 3)
    ]
    triage = GithubTriage(start=None, end=None, results=[RepoResult("o/r", issues=issues)])

    path = tmp_path / "report.html"
    await write_report(path, {"github": triage}, FetchMode.todo, "T", "sub", persistor)

    sections = {
        name: {i.key: i.flags for i in section}
        for name, section in todo_sections(await triage.report_items(persistor))
    }
    assert sections["Bugs gone"] == {"o/r#2": {Flag.GONE}}
    assert sections["No owner"]["o/r#3"] == {Flag.NEW}
    assert 'id="github-o/r#1"' in path.read_text()
