"""Tests for the agent's host tools, tool choice and loop supervisor — all offline."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from startriage.ai.phases import BugFacts
from startriage.ai.search import search_launchpad
from startriage.ai.supervisor import ABORTED, Supervisor
from startriage.ai.tool_choice import format_tools, resolve_tools, save_tools
from startriage.ai.tools import (
    REGISTRY,
    ReadOnlyHTTP,
    Risk,
    ToolContext,
    ToolError,
    ToolSpec,
    build_tools,
    call_tool,
    enabled_tool_names,
)
from startriage.ai.tools.base import MAX_TOOL_OUTPUT, PAGE_LINES, page_text
from startriage.ai.tools.http import NetworkError, check_public_url
from startriage.ai.tools.launchpad import model_payload, series_table
from startriage.ai.tools.web import html_to_text
from startriage.config import AIConfig, StarTriageConfig, load_config

# --- registry & choice -------------------------------------------------------


def test_registry_defaults():
    """Programs only run when the user enables them; so does fetching arbitrary URLs."""
    exec_tools = {name for name, tool in REGISTRY.items() if tool.risk is Risk.exec}
    assert exec_tools == {"fetch_source", "patch_applies"}
    enabled = set(enabled_tool_names(AIConfig()))
    assert not enabled & (exec_tools | {"fetch_url"})
    assert "lp_get_bug" in enabled
    for tool in REGISTRY.values():
        assert tool.parameters["type"] == "object"
        assert set(tool.parameters.get("required", [])) <= tool.parameters["properties"].keys()


def test_enabled_tool_names_follows_config_and_ignores_unknown(caplog):
    cfg = AIConfig(tools={"fetch_url": True, "verify_url": False, "gone_tool": True})
    names = enabled_tool_names(cfg)
    assert "fetch_url" in names
    assert "verify_url" not in names
    assert "lp_get_bug" in names
    assert "gone_tool" in caplog.text


def test_local_source_tools_need_fetch_source():
    assert "grep_source" not in enabled_tool_names(AIConfig(tools={"grep_source": True}))
    names = enabled_tool_names(AIConfig(tools={"fetch_source": True, "grep_source": True}))
    assert {"fetch_source", "grep_source"} <= set(names)
    assert "read_source_file" not in names


def test_format_tools_flags_the_tools_that_run_programs():
    lines = format_tools({"lp_get_bug"}).splitlines()
    assert any("[x] lp_get_bug" in line for line in lines)
    for name in ("fetch_source", "patch_applies"):
        line = next(line for line in lines if f" {name}" in line)
        assert "[ ]" in line and "[runs programs on this machine]" in line


def test_save_tools_round_trips_through_config(tmp_path):
    path = tmp_path / "startriage.toml"
    path.write_text('[ai]\nprovider = "copilot"\n')
    save_tools({"lp_get_bug", "fetch_url"}, path)
    config = load_config(path)
    assert config.ai.provider == "copilot"
    assert config.ai.tools is not None
    assert config.ai.tools["fetch_url"] is True
    assert config.ai.tools["lp_search_bugs"] is False
    assert set(config.ai.tools) == set(REGISTRY)


def test_resolve_tools_without_tty_uses_defaults_and_saves_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    config = StarTriageConfig()
    path = tmp_path / "startriage.toml"
    resolve_tools(config, path)
    assert config.ai.tools is None
    assert not path.exists()
    assert "startriage config ai-tools" in caplog.text


# --- call_tool & paging --------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_bounds_output_and_reports_errors():
    async def big(_args):
        return "x" * (MAX_TOOL_OUTPUT + 10)

    async def refuses(_args):
        raise ToolError("nope")

    async def crashes(_args):
        raise RuntimeError("boom")

    text, ok = await call_tool(ToolSpec("big", "", {}, big), {})
    assert ok
    assert "truncated, 10 more characters" in text
    assert await call_tool(ToolSpec("r", "", {}, refuses), {}) == ("nope", False)
    text, ok = await call_tool(ToolSpec("c", "", {}, crashes), {})
    assert not ok
    assert "RuntimeError: boom" in text


def test_page_text_pages_and_greps():
    text = "\n".join(f"line {n}" for n in range(1, PAGE_LINES + 51))
    first = page_text(text)
    assert first.startswith(f"[lines 1-{PAGE_LINES} of {PAGE_LINES + 50}; continue with offset={PAGE_LINES}]")
    last = page_text(text, offset=PAGE_LINES)
    assert last.splitlines()[0] == f"[lines {PAGE_LINES + 1}-{PAGE_LINES + 50} of {PAGE_LINES + 50}]"
    assert page_text(text, pattern=r"^LINE 7$") == "7: line 7"
    assert "No line" in page_text(text, pattern="absent")
    with pytest.raises(ToolError, match="invalid regular expression"):
        page_text(text, pattern="(")


def test_html_to_text_drops_scripts_and_tags():
    html = "<html><script>var x=1;</script><p>Hello <b>world</b></p><div>bye</div></html>"
    assert html_to_text(html) == "Hello world\nbye"


# --- ReadOnlyHTTP -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://169.254.169.254/x",
        "ftp://8.8.8.8/",
    ],
)
@pytest.mark.asyncio
async def test_check_public_url_rejects(url):
    with pytest.raises(ToolError):
        await check_public_url(url)


@pytest.mark.asyncio
async def test_check_public_url_accepts_public_address():
    await check_public_url("https://8.8.8.8/")


class _Content:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Response:
    def __init__(self, status=200, body=b"", headers=None, content_type="text/plain"):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.charset = None
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_http_rechecks_redirects():
    session = _Session(_Response(302, headers={"Location": "http://127.0.0.1/admin"}))
    http = ReadOnlyHTTP(session)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ToolError, match="non-public"):
        await http.get("http://8.8.8.8/start")
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_http_bounds_body_and_scopes_credentials():
    session = _Session(_Response(body=b"a" * 100), _Response(body=b"b"))
    http = ReadOnlyHTTP(session, credentials={"8.8.8.8": {"Authorization": "secret"}})  # ty: ignore[invalid-argument-type]
    resp = await http.get("https://8.8.8.8/x", max_body=10)
    assert resp.body == b"a" * 10
    assert resp.truncated
    assert session.requests[0][2]["headers"] == {"Authorization": "secret"}
    await http.get("http://8.8.8.8/x")  # plain http: no credentials
    assert session.requests[1][2]["headers"] == {}
    assert all(kwargs["allow_redirects"] is False for *_, kwargs in session.requests)


@pytest.mark.asyncio
async def test_http_post_needs_allow_list():
    http = ReadOnlyHTTP(_Session(_Response()), allowed_posts=frozenset({"https://8.8.8.8/soap"}))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ToolError, match="not allowed"):
        await http.post("https://8.8.8.8/other", b"", {})
    resp = await http.post("https://8.8.8.8/soap", b"<q/>", {})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_http_network_errors_are_tool_errors():
    class _Timeout(_Session):
        def request(self, method, url, **kwargs):
            raise TimeoutError

    http = ReadOnlyHTTP(_Timeout())  # ty: ignore[invalid-argument-type]
    with pytest.raises(NetworkError) as excinfo:
        await http.head("https://8.8.8.8/x")
    assert str(excinfo.value) == "HEAD https://8.8.8.8/x failed: TimeoutError"


# --- Launchpad tools ----------------------------------------------------------


class _Attachment:
    def __init__(self, title: str, data: bytes):
        self.title = title
        self.type = "Unspecified"
        self.data = SimpleNamespace(open=lambda: io.BytesIO(data))


class _Source:
    def __init__(self):
        self.kwargs: dict = {}

    def searchTasks(self, **kwargs):  # noqa: N802 (launchpadlib name)
        self.kwargs = kwargs
        return [
            SimpleNamespace(
                bug_link="https://api.launchpad.net/devel/bugs/42",
                title='Bug #42 in pkg (Ubuntu): "crash"',
                status="New",
                importance="High",
                date_created=datetime(2024, 1, 2, tzinfo=UTC),
            )
        ]


def _fake_lp():
    source = _Source()
    bugs = {
        1: SimpleNamespace(private=True),
        2: SimpleNamespace(
            private=False,
            attachments=[
                _Attachment("log.txt", b"ok\nERROR: disk full\nok\n"),
                _Attachment("core", b"\x7fELF\0\0"),
            ],
        ),
    }
    distro = SimpleNamespace(getSourcePackage=lambda name: source if name == "pkg" else None)
    return SimpleNamespace(bugs=bugs, distributions={"ubuntu": distro}), source


async def _run(name: str, lp, **args):
    tool = build_tools(ToolContext(lp=lp), [name])[0]
    return await call_tool(tool, args)


@pytest.mark.asyncio
async def test_lp_tools_refuse_private_and_missing_bugs():
    lp, _ = _fake_lp()
    text, ok = await _run("lp_get_bug", lp, bug=1)
    assert not ok
    assert "private" in text
    text, ok = await _run("lp_get_attachment", lp, bug="#1", index=0)
    assert not ok
    assert "private" in text
    text, ok = await _run("lp_get_bug", lp, bug=3)
    assert (ok, text) == (False, "bug 3 does not exist")
    text, ok = await _run("lp_get_bug", lp, bug="abc")
    assert not ok
    assert "must be a Launchpad bug number" in text


def test_a_long_bug_keeps_its_newest_comments_and_pages_back():
    comments = [{"author": "a", "text": f"comment {n} " + "x" * 2000} for n in range(1, 41)]
    payload = {"number": 7, "description": "it broke", "private": False, "comments": comments}
    out = model_payload(payload, max_chars=20_000)
    numbers = [c["n"] for c in out["comments"]]
    assert numbers[-1] == 40
    assert len(json.dumps(out, indent=1)) <= 20_000
    assert out["comments_omitted"] == numbers[0] - 1
    assert out["older_comments"] == f"call lp_get_bug with before={numbers[0]} to read them"
    assert "private" not in out
    older = model_payload(payload, before=numbers[0], max_chars=20_000)
    assert older["comments"][-1]["n"] == numbers[0] - 1


@pytest.mark.asyncio
async def test_lp_get_attachment_greps_and_refuses_binary():
    lp, _ = _fake_lp()
    assert await _run("lp_get_attachment", lp, bug=2, index=0, pattern="error") == (
        "2: ERROR: disk full",
        True,
    )
    text, ok = await _run("lp_get_attachment", lp, bug=2, index=1)
    assert not ok
    assert "binary" in text
    text, ok = await _run("lp_get_attachment", lp, bug=2, index=5)
    assert not ok
    assert "has 2 attachments" in text


@pytest.mark.asyncio
async def test_lp_search_bugs_public_only():
    lp, source = _fake_lp()
    text, ok = await _run("lp_search_bugs", lp, package="pkg", text="crash", include_closed=True)
    assert ok
    assert '"bug": "42"' in text
    assert source.kwargs["information_type"] == ["Public", "Public Security"]
    assert "Fix Released" in source.kwargs["status"]
    await _run("lp_search_bugs", lp, package="pkg", text="crash")
    assert "Fix Released" not in source.kwargs["status"]
    text, ok = await _run("lp_search_bugs", lp, package="nope", text="x")
    assert not ok


def _series_lp():
    def series(name, version, status):
        return SimpleNamespace(name=name, version=version, status=status, active=True)

    def pub(series, pocket, version):
        link = f"https://api.launchpad.net/devel/ubuntu/{series}"
        return SimpleNamespace(distro_series_link=link, pocket=pocket, source_package_version=version)

    published = {
        "pkg": [
            pub("resolute", "Release", "2.0-1"),
            pub("noble", "Updates", "1.0-1ubuntu0.1"),
            pub("noble", "Security", "1.0-1ubuntu0.1"),
            pub("noble", "Release", "1.0-1"),
            pub("noble", "Release", "0.9-1"),  # older: ignored
        ]
    }
    archive = SimpleNamespace(getPublishedSources=lambda source_name, **_: published.get(source_name, []))
    ubuntu = SimpleNamespace(
        current_series=SimpleNamespace(name="resolute"),
        series_collection=[
            series("resolute", "26.04", "Active Development"),
            series("noble", "24.04", "Supported"),
            SimpleNamespace(name="warty", active=False),
        ],
        main_archive=archive,
    )
    return SimpleNamespace(distributions={"ubuntu": ubuntu})


def test_series_table():
    table = series_table(_series_lp(), "pkg")
    assert list(table) == ["resolute", "noble"]
    assert table["resolute"]["development"] is True
    assert table["noble"] == {
        "version": "24.04",
        "status": "Supported",
        "development": False,
        "published": {"Updates": "1.0-1ubuntu0.1", "Security": "1.0-1ubuntu0.1", "Release": "1.0-1"},
    }


@pytest.mark.asyncio
async def test_search_launchpad_ranks_and_excludes_the_bug_itself():
    lp, source = _fake_lp()
    facts = BugFacts(package="pkg", symptoms="crash", search_terms=["crash", "segfault"])
    candidates, queries = await search_launchpad(lp, facts, "7", ["absent"])
    assert queries == 3  # 2 terms on pkg, then 'absent' fails on its first term
    assert [(c.ref, c.hits) for c in candidates] == [("42", 2)]
    assert candidates[0].url == "https://bugs.launchpad.net/bugs/42"
    assert "Fix Released" in source.kwargs["status"]
    assert await search_launchpad(lp, facts, "42", []) == ([], 2)
    assert await search_launchpad(None, facts, "7", []) == ([], 0)


@pytest.mark.asyncio
async def test_lp_tools_are_serialized():
    # launchpadlib shares one TLS connection; parallel calls corrupt it.
    import threading
    import time

    active, peak, guard = 0, 0, threading.Lock()

    class _Bugs:
        def __getitem__(self, number):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            raise KeyError(number)

    lp = SimpleNamespace(bugs=_Bugs())
    await asyncio.gather(*(_run("lp_get_bug", lp, bug=n) for n in range(4)))
    assert peak == 1


# --- supervisor -------------------------------------------------------------


def test_supervisor_denies_identical_calls_then_aborts():
    sup = Supervisor()
    results = [sup.check("t", {"a": 1}) for _ in range(5)]
    assert results[:2] == [None, None]
    assert results[2] is not None
    assert "identical call 3 times" in results[2]
    assert results[4] == ABORTED
    assert sup.aborted
    assert sup.check("other", {}) == ABORTED


def test_supervisor_denies_crawling():
    sup = Supervisor()
    results = [sup.check("page", {"offset": n}) for n in range(6)]
    assert results[:5] == [None] * 5
    assert results[5] is not None
    assert "differing only in numbers" in results[5]
    assert not sup.aborted
