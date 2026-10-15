"""Tools over a source package fetched into the run's scratch directory.

``fetch_source`` runs ``pull-lp-source`` (an exec tool, off by default);
``grep_source`` and ``read_source_file`` then search and read the unpacked
tree. Every path the model gives is relative to the fetched sources and may
not leave them (no absolute paths, ``..`` or symlinks pointing outside).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .base import MAX_MATCHES, Risk, ToolContext, ToolDef, ToolError, int_arg, page_text, str_arg
from .source import _PACKAGE_PARAM, _package

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 600
MAX_FILE_SIZE = 1_000_000
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~:-]*$")
_SKIP_DIRS = {".git", ".pc", ".bzr", ".svn"}


def source_root(ctx: ToolContext) -> Path:
    if ctx.workdir is None:
        raise ToolError("no scratch directory in this run")
    root = ctx.workdir / "src"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def safe_path(root: Path, path: str) -> Path:
    """``path`` inside ``root``, refusing anything that resolves outside it."""
    if "\0" in path:
        raise ToolError("invalid path")
    rel = PurePosixPath(path.strip() or ".")
    if rel.is_absolute():
        raise ToolError(f"{path!r} is absolute: paths are relative to the fetched sources (see fetch_source)")
    full = (root / rel).resolve()
    if not full.is_relative_to(root):
        raise ToolError(f"{path!r} is outside the fetched sources")
    return full


def _label(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9.+~-]", "_", ref or "devel")


async def _pull(binary: str, package: str, ref: str, cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "--no-conf",
        "--",
        package,
        *([ref] if ref else []),
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), FETCH_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"pull-lp-source took more than {FETCH_TIMEOUT}s") from None
    return proc.returncode or 0, out.decode(errors="replace")


async def fetch_source(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = source_root(ctx)
    package = _package(str_arg(args, "package"))
    ref = str_arg(args, "ref", required=False)
    if ref == "devel":
        ref = ""
    if ref and not _REF.match(ref):
        raise ToolError(f"ref must be a series name or a version, got {ref!r}")
    target = root / f"{package}_{_label(ref)}"
    if target.is_dir():
        return f"{package} ({ref or 'devel'}) is already in {target.name}/."
    binary = shutil.which("pull-lp-source")
    if binary is None:
        raise ToolError("pull-lp-source is not installed (it is in the ubuntu-dev-tools package)")
    staging = Path(tempfile.mkdtemp(prefix=".fetch-", dir=root))
    try:
        logger.info("Fetching the source of %s %s", package, ref or "devel")
        status, output = await _pull(binary, package, ref, staging)
        trees = [d for d in staging.iterdir() if d.is_dir() and not d.is_symlink()]
        if status or len(trees) != 1:
            tail = "\n".join(output.strip().splitlines()[-15:])
            raise ToolError(f"pull-lp-source failed (exit {status}):\n{tail}")
        dscs = sorted(staging.glob("*.dsc"))
        version = dscs[0].stem.split("_", 1)[-1] if dscs else "?"
        trees[0].rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    count = sum(len(files) for _, _, files in os.walk(target))
    return (
        f"Fetched {package} {version} ({count} files) into {target.name}/. Search it with grep_source "
        f"and read files with read_source_file, with paths starting with {target.name}/."
    )


def _grep(root: Path, start: Path, regex: re.Pattern[str], glob: str) -> list[str]:
    matches: list[str] = []
    files = [start] if start.is_file() else []
    if start.is_dir():
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            files += [Path(dirpath, name) for name in sorted(filenames)]
    for file in files:
        rel = file.relative_to(root).as_posix()
        if glob and not (fnmatch.fnmatch(file.name, glob) or fnmatch.fnmatch(rel, glob)):
            continue
        if file.is_symlink() or not file.is_file() or file.stat().st_size > MAX_FILE_SIZE:
            continue
        data = file.read_bytes()
        if b"\0" in data[:8192]:
            continue
        for n, line in enumerate(data.decode(errors="replace").splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{n}: {line.strip()[:300]}")
                if len(matches) > MAX_MATCHES:
                    return matches
    return matches


async def grep_source(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = source_root(ctx)
    pattern = str_arg(args, "pattern")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc
    start = safe_path(root, str_arg(args, "path", required=False))
    if not start.exists():
        raise ToolError(f"{start.relative_to(root)} does not exist: fetch the package first (fetch_source)")
    glob = str_arg(args, "glob", required=False)
    matches = await asyncio.to_thread(_grep, root, start, regex, glob)
    if not matches:
        return f"No match for {pattern!r}."
    if len(matches) > MAX_MATCHES:
        return "\n".join([*matches[:MAX_MATCHES], "[more matches; narrow the pattern, path or glob]"])
    return "\n".join(matches)


async def read_source_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = source_root(ctx)
    path = safe_path(root, str_arg(args, "path", required=False))
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        rel = path.relative_to(root).as_posix()
        return f"{len(entries)} entries in {rel}/:\n" + "\n".join(entries)
    if not path.is_file():
        raise ToolError(f"{path.relative_to(root)} does not exist")
    if path.stat().st_size > 5 * MAX_FILE_SIZE:
        raise ToolError(f"{path.relative_to(root)} is too large to read")
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{path.relative_to(root)} is a binary file")
    pattern = str_arg(args, "pattern", required=False) or None
    return page_text(data.decode(errors="replace"), pattern=pattern, offset=int_arg(args, "offset"))


_PATH_PARAM = {
    "type": "string",
    "description": "Path under the fetched sources, e.g. nginx_noble/src/core (default: everything)",
}

TOOLS = [
    ToolDef(
        name="fetch_source",
        group="local source",
        risk=Risk.exec,
        default=False,
        summary="Download and unpack a source package (runs pull-lp-source)",
        description=(
            "Download and unpack a source package into this run's scratch directory (pull-lp-source), "
            "to search the whole tree with grep_source. Slower than source_file: use it when you "
            "must search many files. ref is a series name or a version (default: devel)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "ref": {"type": "string", "description": "Series (e.g. noble) or version (default: devel)"},
            },
            "required": ["package"],
        },
        run=fetch_source,
    ),
    ToolDef(
        name="grep_source",
        group="local source",
        risk=Risk.read,
        default=False,
        summary="Search the fetched sources with a regular expression",
        description=(
            "Search the sources fetched with fetch_source for a regular expression (Python syntax, "
            "case-sensitive). Returns path:line: text, at most 200 matches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": _PATH_PARAM,
                "glob": {"type": "string", "description": "Only files matching this glob, e.g. *.c"},
            },
            "required": ["pattern"],
        },
        run=grep_source,
    ),
    ToolDef(
        name="read_source_file",
        group="local source",
        risk=Risk.read,
        default=False,
        summary="Read a file (or list a directory) of the fetched sources",
        description=(
            "Read a file of the sources fetched with fetch_source, 300 lines at a time from offset, "
            "or only the lines matching pattern. A directory is listed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH_PARAM,
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
                "pattern": {"type": "string", "description": "Only return lines matching this regex"},
            },
            "required": ["path"],
        },
        run=read_source_file,
    ),
]

# These only make sense once fetch_source has unpacked something.
NEEDS_FETCH = {"grep_source", "read_source_file"}
