"""Tests for the patch checker (GNU patch) and the local source tools."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from startriage.ai.tools import ToolContext, ToolError, build_tools, call_tool
from startriage.ai.tools.local import safe_path
from startriage.ai.tools.patch import (
    ApplyState,
    FileCheck,
    check_file,
    guess_strip,
    overall,
    parse_patch,
    strip_path,
)

FILE = "".join(f"line {n}\n" for n in range(1, 11))
DIFF = "--- a/src/a.c\n+++ b/src/a.c\n@@ -4,3 +4,3 @@\n line 4\n-line 5\n+line five\n line 6\n"


def test_parse_patch_reads_files_hunks_and_creations():
    text = (
        "From: someone\n\nSubject: fix\n---\n"
        + DIFF
        + "--- /dev/null\n+++ b/debian/new.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        + "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n\\ No newline at end of file\n"
    )
    files = parse_patch(text)
    assert [(f.path, f.creates, f.deletes) for f in files] == [
        ("a/src/a.c", False, False),
        ("b/debian/new.txt", True, False),
        ("a/old.txt", False, True),
    ]
    # Only the hunks reach GNU patch, under fixed names.
    assert (
        files[2].diff() == "--- a/file\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n\\ No newline at end of file\n"
    )
    assert guess_strip(files) == 1
    assert strip_path("a/src/a.c", 1) == "src/a.c"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("just words\n", "no unified diff"),
        ("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n-b\n", "truncated"),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n*b\n", "malformed hunk"),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,1 @@\n a\n+b\n c\n", "line counts"),
    ],
)
def test_parse_patch_rejects_broken_diffs(text, error):
    with pytest.raises(ToolError, match=error):
        parse_patch(text)


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch is not installed")
@pytest.mark.asyncio
async def test_check_file_reports_what_gnu_patch_finds():
    patch = parse_patch(DIFF)[0]

    async def state(text: str | None) -> tuple[ApplyState, str]:
        check = await check_file(text, patch)
        return check.state, check.detail

    assert await state(FILE) == (ApplyState.clean, "")
    assert await state("x\n" + FILE) == (ApplyState.offset, "#1 at line 5 offset +1")
    assert (await state(FILE.replace("line 4", "line four")))[0] is ApplyState.fuzz
    assert await state(FILE.replace("line 5", "line five")) == (ApplyState.applied, "")
    assert await state("other\n" * 10) == (ApplyState.fails, "1 of 1 hunks do not apply: #1 near line 4")
    assert await state(None) == (ApplyState.fails, "file not found")
    new = parse_patch("--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1 @@\n+one\n")[0]
    assert (await check_file(None, new)).state is ApplyState.clean
    assert (await check_file("something\n", new)).state is ApplyState.fails


def test_overall_state_is_the_worst_and_partly_applied_fails():
    def check(state: ApplyState) -> FileCheck:
        return FileCheck(path="f", state=state)

    assert overall([check(ApplyState.clean), check(ApplyState.offset)]) is ApplyState.offset
    assert overall([check(ApplyState.applied), check(ApplyState.applied)]) is ApplyState.applied
    assert overall([check(ApplyState.applied), check(ApplyState.clean)]) is ApplyState.fails


# --- local source tools ---------------------------------------------------------------


def test_safe_path_stays_inside_the_root(tmp_path):
    root = (tmp_path / "src").resolve()
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "escape").symlink_to("/etc")
    assert safe_path(root, "pkg/a.c") == root / "pkg" / "a.c"
    assert safe_path(root, "") == root
    for bad in ("/etc/passwd", "../outside", "pkg/../../outside", "pkg/escape/passwd", "pkg/\0"):
        with pytest.raises(ToolError):
            safe_path(root, bad)


def _tools(tmp_path: Path) -> dict:
    names = ["fetch_source", "grep_source", "read_source_file"]
    return {t.name: t for t in build_tools(ToolContext(workdir=tmp_path), names)}


@pytest.fixture
def fake_pull(tmp_path, monkeypatch) -> Path:
    """A pull-lp-source that unpacks a tiny tree and logs its arguments."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "pull-lp-source"
    script.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$ARGS_LOG"\n'
        "mkdir -p hello-2.10/src && touch hello_2.10-3.dsc\n"
        "printf 'int main(void)\\n{\\n  puts(\"hi\");\\n}\\n' > hello-2.10/src/hello.c\n"
        "printf 'binary\\0data' > hello-2.10/blob\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "args.log"
    monkeypatch.setenv("ARGS_LOG", str(log))
    return log


@pytest.mark.asyncio
async def test_fetch_grep_and_read_a_source_tree(tmp_path, fake_pull):
    tools = _tools(tmp_path / "work")
    text, ok = await call_tool(tools["fetch_source"], {"package": "hello", "ref": "noble"})
    assert ok, text
    assert text.startswith("Fetched hello 2.10-3 (2 files) into hello_noble/.")
    assert fake_pull.read_text() == "--no-conf -- hello noble\n"
    text, ok = await call_tool(tools["fetch_source"], {"package": "hello", "ref": "noble"})
    assert (ok, text) == (True, "hello (noble) is already in hello_noble/.")
    assert fake_pull.read_text().count("\n") == 1

    text, ok = await call_tool(tools["grep_source"], {"pattern": r"puts\("})
    assert (ok, text) == (True, 'hello_noble/src/hello.c:3: puts("hi");')
    text, ok = await call_tool(tools["grep_source"], {"pattern": "data"})
    assert (ok, text) == (True, "No match for 'data'.")  # binaries are skipped

    text, ok = await call_tool(tools["read_source_file"], {"path": "hello_noble"})
    assert (ok, text) == (True, "2 entries in hello_noble/:\nblob\nsrc/")
    text, ok = await call_tool(tools["read_source_file"], {"path": "hello_noble/src/hello.c"})
    assert ok and "puts" in text
    text, ok = await call_tool(tools["read_source_file"], {"path": "hello_noble/blob"})
    assert not ok and "binary" in text
    text, ok = await call_tool(tools["read_source_file"], {"path": "../args.log"})
    assert not ok and "outside the fetched sources" in text


@pytest.mark.asyncio
async def test_fetch_source_refuses_odd_refs_and_needs_a_workdir(tmp_path, fake_pull):
    text, ok = await call_tool(_tools(tmp_path)["fetch_source"], {"package": "hello", "ref": "-x"})
    assert not ok and "ref must be a series name or a version" in text
    tool = build_tools(ToolContext(), ["fetch_source"])[0]
    text, ok = await call_tool(tool, {"package": "hello"})
    assert not ok and "no scratch directory" in text
    assert not fake_pull.exists()
