"""Check whether a patch applies to a package, with GNU ``patch --dry-run``.

The files come from git-ubuntu with the package's own patches applied
(``applied/`` refs). The diff is parsed here only to know which files to
fetch and to hand ``patch`` nothing but validated unified hunks, one file at a
time, with the target file given explicitly (the paths in the diff are never
used). Running ``patch`` makes this an exec tool, off unless enabled.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .base import Risk, ToolContext, ToolDef, ToolError, int_arg, need_http, str_arg
from .forges import patch_url
from .http import ReadOnlyHTTP
from .source import checked_ref, read_file, resolve_ref

MAX_FILES = 30
PATCH_TIMEOUT = 30
DEV_NULL = "/dev/null"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GNU_HUNK = re.compile(
    r"^Hunk #(\d+) (succeeded|FAILED) at (\d+)(?: with fuzz (\d+))?(?: \(offset (-?\d+) lines?\))?\."
)


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    # Each hunk: its @@ line, then its lines as in the diff.
    hunks: list[list[str]]

    @property
    def creates(self) -> bool:
        return self.old_path == DEV_NULL

    @property
    def deletes(self) -> bool:
        return self.new_path == DEV_NULL

    @property
    def path(self) -> str:
        return self.new_path if self.creates else self.old_path

    def diff(self) -> str:
        """This file's hunks as a diff for ``patch`` (fixed names: the target is given apart)."""
        lines = ["--- a/file", f"+++ {DEV_NULL if self.deletes else 'b/file'}"]
        for hunk in self.hunks:
            lines += hunk
        return "\n".join(lines) + "\n"


def _header_path(line: str) -> str:
    name = line[4:].split("\t", 1)[0].strip()
    if len(name) > 1 and name[0] == name[-1] == '"':
        name = name[1:-1]
    return name


def _lines(text: str) -> list[str]:
    """Lines without their ``\\n`` (a ``\\r`` stays, as GNU patch compares bytes)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_patch(text: str) -> list[FilePatch]:
    """The files and hunks of a unified diff (git, quilt or debdiff)."""
    lines = _lines(text)
    files: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            files.append(FilePatch(_header_path(line), _header_path(lines[i + 1]), []))
            i += 2
            continue
        match = _HUNK.match(line)
        if not (match and files):
            i += 1
            continue
        old_len = 1 if match.group(2) is None else int(match.group(2))
        new_len = 1 if match.group(4) is None else int(match.group(4))
        body: list[str] = [line]
        old_seen = new_seen = 0
        i += 1
        while old_seen < old_len or new_seen < new_len:
            if i >= len(lines):
                raise ToolError(f"the hunk {match.group(0)} of {files[-1].path} is truncated")
            row = lines[i]
            i += 1
            if row.startswith("\\"):
                body.append(row)  # "\ No newline at end of file"
                continue
            if row == "" or row[0] == "\t":
                row = " " + row  # a blank context line whose space was eaten
            if row[0] not in " -+":
                raise ToolError(f"malformed hunk {match.group(0)} in {files[-1].path}: {row[:60]!r}")
            old_seen += row[0] != "+"
            new_seen += row[0] != "-"
            body.append(row)
        if old_seen != old_len or new_seen != new_len:
            raise ToolError(f"the line counts of hunk {match.group(0)} in {files[-1].path} are wrong")
        while i < len(lines) and lines[i].startswith("\\"):
            body.append(lines[i])
            i += 1
        files[-1].hunks.append(body)
    files = [f for f in files if f.hunks]
    if not files:
        raise ToolError("no unified diff found (it needs ---/+++ file headers and @@ hunks)")
    return files


def strip_path(path: str, strip: int) -> str:
    if path == DEV_NULL:
        return path
    parts = path.lstrip("/").split("/")
    if strip >= len(parts):
        raise ToolError(f"cannot strip {strip} components from {path!r}")
    return "/".join(parts[strip:])


def guess_strip(files: list[FilePatch]) -> int:
    """1 for ``a/`` and ``b/`` prefixes (git, most patches), else 0."""
    olds = [f.old_path for f in files if not f.creates]
    news = [f.new_path for f in files if not f.deletes]
    return int(all(p.startswith("a/") for p in olds) and all(p.startswith("b/") for p in news))


class ApplyState(StrEnum):
    clean = "clean"  # every hunk at its line
    offset = "offset"  # some hunks moved, context intact
    fuzz = "fuzz"  # some context lines ignored
    applied = "already applied"  # the reverse applies: the change is already there
    fails = "fails"  # some hunks do not apply, or a file is missing


_RANK = [ApplyState.clean, ApplyState.offset, ApplyState.fuzz, ApplyState.applied, ApplyState.fails]


class FileCheck(BaseModel):
    path: str
    state: ApplyState
    detail: str = ""


class PatchCheck(BaseModel):
    """How a patch applies to one tree."""

    ref: str
    state: ApplyState
    files: list[FileCheck]
    # Set when the patches-applied tree was stale and the plain one was checked.
    note: str = ""

    @property
    def patches_applied(self) -> bool:
        return not self.note

    def describe(self) -> str:
        lines = [f"{self.ref}: {self.state.value}" + (f" [{self.note}]" if self.note else "")]
        lines += [f"  {f.path}: {f.state.value}" + (f" ({f.detail})" if f.detail else "") for f in self.files]
        return "\n".join(lines)


async def _gnu_patch(text: str, diff: str) -> tuple[int, str]:
    """Exit status and output of ``patch --dry-run`` of ``diff`` on a file holding ``text``."""
    binary = shutil.which("patch")
    if binary is None:
        raise ToolError("GNU patch is not installed")
    with tempfile.TemporaryDirectory(prefix="startriage-patch-") as tmp:
        target, diff_file = Path(tmp, "file"), Path(tmp, "diff")
        target.write_text(text, errors="surrogateescape")
        diff_file.write_text(diff, errors="surrogateescape")
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--dry-run",
            "--batch",
            "--forward",
            "--unified",
            "--input",
            str(diff_file),
            str(target),
            cwd=tmp,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), PATCH_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError(f"patch took more than {PATCH_TIMEOUT}s") from None
    return proc.returncode or 0, out.decode(errors="replace")


def _file_check(path: str, hunks: int, status: int, output: str) -> FileCheck:
    """Read GNU patch's verdict on one file."""
    if "Reversed (or previously applied) patch detected" in output:
        return FileCheck(path=path, state=ApplyState.applied)
    results = [_GNU_HUNK.match(line) for line in output.splitlines()]
    matched = [m for m in results if m]
    failed = [m for m in matched if m[2] == "FAILED"]
    if status > 1 or (status == 1 and not failed):
        last = output.strip().splitlines()[-1:] or [f"exit status {status}"]
        return FileCheck(path=path, state=ApplyState.fails, detail=f"patch: {last[0]}")
    if failed:
        where = ", ".join(f"#{m[1]} near line {m[3]}" for m in failed)
        return FileCheck(
            path=path, state=ApplyState.fails, detail=f"{len(failed)} of {hunks} hunks do not apply: {where}"
        )
    notes = [
        f"#{m[1]} at line {m[3]}"
        + (f" offset {int(m[5]):+d}" if m[5] else "")
        + (f" fuzz {m[4]}" if m[4] else "")
        for m in matched
    ]
    state = (
        ApplyState.fuzz
        if any(m[4] for m in matched)
        else ApplyState.offset
        if any(m[5] for m in matched)
        else ApplyState.clean
    )
    return FileCheck(path=path, state=state, detail="; ".join(notes))


async def check_file(text: str | None, patch: FilePatch) -> FileCheck:
    """How ``patch`` applies to a file holding ``text`` (``None``: the file does not exist)."""
    path = patch.path
    if patch.creates:
        if text:
            return FileCheck(path=path, state=ApplyState.fails, detail="the patch creates it, but it exists")
        return FileCheck(path=path, state=ApplyState.clean, detail="new file")
    if text is None:
        return FileCheck(path=path, state=ApplyState.fails, detail="file not found")
    status, output = await _gnu_patch(text, patch.diff())
    return _file_check(path, len(patch.hunks), status, output)


def overall(files: list[FileCheck]) -> ApplyState:
    states = {f.state for f in files}
    if states == {ApplyState.applied}:
        return ApplyState.applied
    if ApplyState.applied in states:
        return ApplyState.fails  # partly applied already
    return max(states, key=_RANK.index)


def applied_ref(ref: str) -> str:
    """The git-ubuntu ref of ``ref`` with the package's patches applied."""
    git_ref = resolve_ref(ref)
    if git_ref.startswith("applied/"):
        return git_ref
    return "applied/" + git_ref.removeprefix("import/")


async def check_patch(
    http: ReadOnlyHTTP, files: list[FilePatch], package: str, ref: str, strip: int
) -> PatchCheck:
    """How ``files`` apply to ``package`` at ``ref`` (patches applied, if git-ubuntu has them)."""
    git_ref, note = await checked_ref(http, package, applied_ref(ref))
    checks = []
    for patch in files[:MAX_FILES]:
        path = strip_path(patch.path, strip)
        try:
            text: str | None = await read_file(http, package, path, git_ref)
        except ToolError as exc:
            if "not found" not in str(exc):
                raise
            text = None
        check = await check_file(text, patch)
        checks.append(check.model_copy(update={"path": path}))
    if len(files) > MAX_FILES:
        checks.append(
            FileCheck(
                path=f"[{len(files) - MAX_FILES} more files]", state=ApplyState.fails, detail="not checked"
            )
        )
    return PatchCheck(ref=git_ref, state=overall(checks), files=checks, note=note)


async def read_patch(http: ReadOnlyHTTP, url: str) -> str:
    """The raw patch behind a commit, pull/merge request or patch URL."""
    raw = patch_url(url)
    resp = await http.get(raw)
    if resp.status >= 400:
        raise ToolError(f"HTTP {resp.status} for {raw}")
    if resp.truncated:
        raise ToolError(f"the patch at {raw} is too large to check")
    return resp.text()


async def patch_applies(ctx: ToolContext, args: dict[str, Any]) -> str:
    http = need_http(ctx)
    package = str_arg(args, "package")
    text = str(args.get("patch") or "")
    url = str_arg(args, "url", required=False)
    if bool(text.strip()) == bool(url):
        raise ToolError("give either patch (the diff text) or url (a commit or patch link)")
    if url:
        text = await read_patch(http, url)
    files = parse_patch(text)
    strip = guess_strip(files) if args.get("strip") in (None, "") else int_arg(args, "strip")
    refs = [r.strip() for r in str_arg(args, "ref", required=False).split(",") if r.strip()] or ["devel"]
    results = [await check_patch(http, files, package, ref, strip) for ref in refs[:4]]
    hunks = sum(len(f.hunks) for f in files)
    header = f"Patch: {len(files)} files, {hunks} hunks (strip {strip}), against {package}:"
    return "\n".join([header, *(r.describe() for r in results)])


TOOLS = [
    ToolDef(
        name="patch_applies",
        group="source",
        risk=Risk.exec,
        default=False,
        summary="Check whether a patch applies to a package version (runs patch --dry-run)",
        description=(
            "Check with 'patch --dry-run' whether a unified diff applies to a source package with "
            "its own patches applied (git-ubuntu applied/ refs). Give the diff text "
            "in 'patch', or a commit/pull request/patch URL in 'url'. Per file: clean, offset (moved), "
            "fuzz (context ignored), already applied, or fails."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Source package name"},
                "patch": {"type": "string", "description": "Unified diff text"},
                "url": {"type": "string", "description": "Commit, pull/merge request or patch URL"},
                "ref": {
                    "type": "string",
                    "description": (
                        "Comma-separated series names or versions (e.g. 'noble,resolute'); default devel"
                    ),
                },
                "strip": {
                    "type": "integer",
                    "description": "Leading path components to strip (like patch -p); default: guessed",
                },
            },
            "required": ["package"],
        },
        run=patch_applies,
    ),
]
