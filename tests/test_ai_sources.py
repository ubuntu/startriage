"""Source, Debian and forge tools against recorded HTTP (tests/fixtures/ai), plus their helpers."""

from __future__ import annotations

import pytest

from startriage.ai import tools as tools_module
from startriage.ai.checks import check_hunk
from startriage.ai.phases import Candidate, ChangelogHit
from startriage.ai.search import changelog_hits, mark_versions, previous_version, search_debian
from startriage.ai.supervisor import OVER_BUDGET, Supervisor
from startriage.ai.tools import ReadOnlyHTTP, ToolContext, ToolError, build_tools, call_tool, debian
from startriage.ai.tools.debian import matches
from startriage.ai.tools.forges import patch_url
from startriage.ai.tools.source import dep14, resolve_ref

from .http_replay import ReplaySession
from .record_ai_fixtures import NOBLE_SG3, TOOL_CALLS


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(tools_module.http, "check_public_url", public)


def _http(name: str) -> tuple[ReadOnlyHTTP, ReplaySession]:
    session = ReplaySession(name)
    return ReadOnlyHTTP(session, allowed_posts=debian.ALLOWED_POSTS), session  # ty: ignore[invalid-argument-type]


async def _call(http: ReadOnlyHTTP, fixture: str, index: int) -> tuple[str, bool]:
    tool, args = TOOL_CALLS[fixture][index]
    return await call_tool(build_tools(ToolContext(http=http), [tool])[0], args)


# --- source (git-ubuntu) --------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("", "ubuntu/devel"),
        ("sid", "debian/sid"),
        ("noble", "ubuntu/noble-devel"),
        ("1:2.0~rc1-1", "import/1%2.0_rc1-1"),
        ("import/1.0~b1", "import/1.0_b1"),
        ("applied/ubuntu/noble-devel", "applied/ubuntu/noble-devel"),
        ("ubuntu/noble-updates", "ubuntu/noble-updates"),
    ],
)
def test_resolve_ref(ref, expected):
    assert resolve_ref(ref) == expected


def test_resolve_ref_rejects_junk_and_dep14_edges():
    with pytest.raises(ToolError, match="cannot read ref"):
        resolve_ref("HEAD~1")
    assert dep14("1.0.") == "1.0.#"
    assert dep14("1..2.lock") == "1.#.2.#lock"


@pytest.mark.asyncio
async def test_source_diff_stats_files_and_quotes_the_diff():
    http, _ = _http("nginx")
    text, ok = await _call(http, "nginx", 0)
    assert ok
    assert text.startswith("3 files changed between 1.24.0-2ubuntu7.17 and 1.24.0-2ubuntu7.18:")
    assert "debian/patches/CVE-2026-42533.patch (+228 -606)" in text


@pytest.mark.asyncio
async def test_unknown_package_is_reported_not_followed_to_the_login_page():
    http, _ = _http("nginx")
    text, ok = await _call(http, "nginx", 3)
    assert not ok
    assert "not imported in git-ubuntu" in text


@pytest.mark.asyncio
async def test_upstream_info_reads_control_watch_and_optional_metadata():
    http, _ = _http("nginx")
    text, ok = await _call(http, "nginx", 4)
    assert ok
    assert '"homepage": "https://nginx.org"' in text
    assert "salsa.debian.org/nginx-team/nginx" in text
    http, _ = _http("sg3-utils")
    text, _ = await _call(http, "sg3-utils", 3)
    assert "sg.danny.cz" in text


@pytest.mark.asyncio
async def test_pkg_changelog_greps_a_series():
    http, _ = _http("sg3-utils")
    text, ok = await _call(http, "sg3-utils", 2)
    assert ok
    assert text.startswith("[2 of 44 entries match")
    assert "LP: #2039279" in text


@pytest.mark.asyncio
async def test_changelog_hits_record_the_oldest_closing_upload():
    http, _ = _http("sg3-utils")
    hits = await changelog_hits(http, "sg3-utils", ["2039279", "1976228", "2137191"], NOBLE_SG3)
    assert {(h.bug, h.series, h.version) for h in hits} == {
        ("2039279", "noble", "1.46-3ubuntu3"),
        ("1976228", "noble", "1.46-3ubuntu2"),
    }
    assert all("LP: #" in h.entry for h in hits)
    assert await previous_version(http, "sg3-utils", "1.46-3ubuntu4.1") == "1.46-3ubuntu4"


def test_mark_versions():
    series = {
        "noble": {"published": {"Release": "1.0-1", "Updates": "1.0-1ubuntu1"}},
        "jammy": {"published": {"Release": "0.9-1", "Proposed": "1.0-2"}},
    }
    lp = Candidate(source="launchpad", ref="7", url="u", title="t", status="s", package="p")
    deb = Candidate(
        source="debian", ref="8", url="u", title="t", status="s", package="p", fixed_version="1.0-1"
    )
    hits = [ChangelogHit(bug="7", series="noble", version="1.0-1ubuntu1", entry="")]
    mark_versions([lp, deb], series, hits)
    assert lp.in_series == ["noble"]
    assert deb.in_series == ["noble"]  # jammy's 1.0-2 is only in -proposed


@pytest.mark.asyncio
async def test_source_list_lists_a_directory_and_refuses_a_file():
    http, _ = _http("source")
    text, ok = await _call(http, "source", 0)
    assert ok
    assert text.startswith("7 entries in src/http/v2 at ubuntu/noble-devel (directories end with /):\n")
    assert "\nngx_http_v2.c\n" in text
    text, ok = await _call(http, "source", 1)
    assert (ok, text) == (False, "README is a file, not a directory: read it with source_file")


@pytest.mark.asyncio
async def test_source_symbols_finds_definitions_newest_first():
    http, _ = _http("source")
    text, ok = await _call(http, "source", 2)
    assert ok
    assert "in Debian's nginx" in text
    assert text.splitlines()[1] == "src/http/ngx_http_script.c:1064 (Debian 1.6.2-5+deb8u5)"
    tool = build_tools(ToolContext(http=http), ["source_symbols"])[0]
    text, ok = await call_tool(tool, {"package": "nginx", "symbol": "a b"})
    assert not ok


@pytest.mark.asyncio
async def test_patch_applies_per_ref_like_gnu_patch():
    """The regex fix is in noble and devel; 1.18.0-6ubuntu14 takes it at an offset, as GNU patch does."""
    http, _ = _http("source")
    text, ok = await _call(http, "source", 3)
    assert ok
    assert text.splitlines() == [
        "Patch: 1 files, 1 hunks (strip 1), against nginx:",
        "applied/ubuntu/noble-devel: already applied",
        "  src/http/ngx_http_script.c: already applied",
        "applied/ubuntu/devel: already applied",
        "  src/http/ngx_http_script.c: already applied",
        "applied/1.18.0-6ubuntu14: offset",
        "  src/http/ngx_http_script.c: offset (#1 at line 1164 offset -38)",
    ]
    text, ok = await _call(http, "source", 4)
    assert ok
    assert "applied/ubuntu/noble-devel: fails" in text
    assert "debian/patches/lp-fix.patch: clean (new file)" in text
    assert "src/nope.c: fails (file not found)" in text


@pytest.mark.asyncio
async def test_a_bugzilla_attachment_is_checked_on_the_plain_tree_when_applied_is_stale():
    """samba's applied/ubuntu/devel is stuck at 3.6.3 and noble has none; the fix is already in devel."""
    http, _ = _http("source")
    text, ok = await _call(http, "source", 6)
    assert ok
    assert "ubuntu/noble-devel: offset [git-ubuntu's applied/ubuntu/noble-devel does not exist" in text
    assert "ubuntu/devel: already applied [git-ubuntu's applied/ubuntu/devel is at 2:3.6.3-2ubuntu2" in text
    assert "WITHOUT the Debian/Ubuntu patches applied]" in text
    assert "source3/printing/print_cups.c: already applied" in text


# --- Debian BTS ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "phrase", "hit"),
    [
        ("upstream rules missing from package", "rules missing", True),
        ("upstream rules present", "rules missing", False),
        ("winbind fails at boot", "winbind crash start", False),
        ("winbind crash at start on boot", "winbind crash start boot", True),
    ],
)
def test_matches(subject, phrase, hit):
    assert matches(subject, phrase) is hit


@pytest.mark.asyncio
async def test_debian_bugs_and_bug_log():
    http, _ = _http("sg3-utils")
    text, ok = await _call(http, "sg3-utils", 0)
    assert ok
    assert '"bug": "1145692"' in text
    assert '"status": "open"' in text
    text, ok = await _call(http, "sg3-utils", 1)
    assert ok
    assert "--- message 0:" in text


@pytest.mark.asyncio
async def test_search_debian_ranks_and_keeps_fixed_version():
    http, _ = _http("sg3-utils")
    found = await search_debian(http, ["sg3-utils"], ["rules missing", "rescan-scsi-bus.sh"])
    assert found[0].ref == "1145692"
    assert all(c.source == "debian" and c.package == "sg3-utils" for c in found)
    by_ref = {c.ref: c for c in found}
    assert by_ref["758926"].fixed_version == "1.38-2"


# --- forges --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_commit_search_and_patch():
    http, _ = _http("nginx")
    text, ok = await _call(http, "nginx", 1)
    assert ok
    assert "buffer overrun protection" in text
    text, ok = await _call(http, "nginx", 2)
    assert ok
    assert "Subject: [PATCH] Rewrite: fixed escaping and possible buffer overrun" in text


@pytest.mark.asyncio
async def test_gitlab_and_bugzilla_search():
    http, _ = _http("forges")
    text, ok = await _call(http, "forges", 0)
    assert ok
    assert "winbind crash on start" in text
    text, ok = await _call(http, "forges", 1)
    assert ok
    assert "CVE-2020-14323" in text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/o/r/commit/abc1234", "https://github.com/o/r/commit/abc1234.patch"),
        ("https://github.com/o/r/pull/12/", "https://github.com/o/r/pull/12.patch"),
        (
            "https://salsa.debian.org/g/p/-/commit/abcdef0",
            "https://salsa.debian.org/g/p/-/commit/abcdef0.patch",
        ),
        ("https://git.kernel.org/x.git/commit/?id=abc", "https://git.kernel.org/x.git/patch/?id=abc"),
        (
            "https://sourceware.org/git/?p=glibc.git;a=commitdiff;h=abc",
            "https://sourceware.org/git/?p=glibc.git;a=patch;h=abc",
        ),
        ("https://example.org/fix.diff", "https://example.org/fix.diff"),
        (
            "https://bugzilla.samba.org/attachment.cgi?id=18821&action=edit",
            "https://bugzilla.samba.org/attachment.cgi?id=18821",
        ),
    ],
)
def test_patch_url(url, expected):
    assert patch_url(url) == expected


def test_patch_url_rejects_other_pages():
    with pytest.raises(ToolError, match="not a recognised"):
        patch_url("https://github.com/o/r/issues/3")


# --- checks and supervisor -------------------------------------------------------


def test_check_hunk():
    diff = "--- a/f\n+++ b/f\n@@\n-old line\n+new line\n context\n"
    assert check_hunk("-old line\n+new line", diff) == []
    assert check_hunk("context only", diff)[0].startswith("hunk must quote")
    assert "'+invented'" in check_hunk("+new line\n+invented", diff)[0]


def test_supervisor_budget_spares_terminal_tools():
    sup = Supervisor(max_calls=2, exempt=frozenset({"submit"}))
    assert sup.check("t", {"i": 1}) is None
    assert sup.check("t", {"i": 2}) is None
    assert sup.check("t", {"i": 3}) == OVER_BUDGET
    assert sup.check("submit", {}) is None
