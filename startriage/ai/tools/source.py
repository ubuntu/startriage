"""Package source tools over git-ubuntu: files, changelogs, diffs, upstream links.

Every Ubuntu source package is imported into git at
``https://git.launchpad.net/ubuntu/+source/<package>``, with a branch per
series and pocket (``ubuntu/devel``, ``ubuntu/noble-updates``,
``ubuntu/noble-devel``), the Debian branches (``debian/sid``) and a tag per
upload (``import/<version>``, with patches applied: ``applied/<version>``).
cgit serves single files (``plain/``) and diffs (``rawdiff/``), so nothing is
cloned or written to disk.
"""

from __future__ import annotations

import fnmatch
import json
import re
import warnings
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import yaml
from debian.changelog import ChangeBlock, Changelog
from debian.deb822 import Deb822
from debian.debian_support import Version
from pydantic import BaseModel

from .base import Risk, ToolContext, ToolDef, ToolError, int_arg, need_http, page_text, str_arg
from .http import ReadOnlyHTTP

CGIT = "https://git.launchpad.net/ubuntu/+source"
CHANGELOG_ENTRIES = 10
MAX_CHANGELOG_MATCHES = 20
MAX_ENTRY_LINES = 60
# Characters of diff returned at once; the rest of the files are listed.
MAX_DIFF = 15_000
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_SERIES = re.compile(r"^[a-z]+$")
_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
# Known GitLab instances (also the hosts gitlab_search may query).
GITLAB_HOSTS = frozenset(
    {
        "salsa.debian.org",
        "gitlab.com",
        "gitlab.archlinux.org",
        "gitlab.freedesktop.org",
        "gitlab.gnome.org",
        "invent.kde.org",
        "gitlab.isc.org",
        "gitlab.alpinelinux.org",
        "code.videolan.org",
    }
)
_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$")
_LP_BUGS = re.compile(r"LP:\s*((?:#\d+[\s,]*)+)", re.I)
# An entry of a cgit directory listing: <a href='/.../plain/<path>[/]?h=<ref>'>name</a>.
_LISTING = re.compile(r"<li><a href='([^'?]*)[^']*'>([^<]*)</a></li>")
SOURCES_API = "https://sources.debian.org/api/ctag/"
MAX_SYMBOLS = 20


def dep14(version: str) -> str:
    """A version as a git tag name (DEP-14), as git-ubuntu writes it."""
    version = version.replace("~", "_").replace(":", "%").replace("..", ".#.")
    if version.endswith("."):
        version += "#"
    if version.endswith(".lock"):
        version = version[: -len(".lock")] + ".#lock"
    return version


def resolve_ref(ref: str) -> str:
    """Turn a version, a series name or a git-ubuntu branch/tag into a git ref.

    ``devel`` (or nothing) is the development series, ``debian`` is Debian
    unstable, a series name (``noble``) is the newest upload in that series,
    and a version (``1.2-3ubuntu1``) is that upload.
    """
    ref = ref.strip()
    if ref in ("", "devel"):
        return "ubuntu/devel"
    if ref in ("debian", "sid", "unstable"):
        return "debian/sid"
    for kind in ("import/", "applied/"):
        if ref.startswith(kind):
            rest = ref[len(kind) :]
            # applied/ubuntu/<branch> is a branch; applied/<version> a tag.
            return ref if rest.startswith(("ubuntu/", "debian/")) else kind + dep14(rest)
    if ref.startswith(("ubuntu/", "debian/")):
        return ref
    if _SERIES.match(ref):
        return f"ubuntu/{ref}-devel"
    if ref[0].isdigit():
        return f"import/{dep14(ref)}"
    raise ToolError(f"cannot read ref {ref!r}: use a version, a series name, 'devel' or 'debian'")


def unapplied_ref(git_ref: str) -> str:
    """``git_ref`` without the patches applied (``applied/ubuntu/X`` -> ``ubuntu/X``)."""
    rest = git_ref.removeprefix("applied/")
    return rest if rest.startswith(("ubuntu/", "debian/")) else "import/" + rest


async def _head_version(http: ReadOnlyHTTP, package: str, git_ref: str) -> str | None:
    try:
        text = await read_file(http, package, "debian/changelog", git_ref)
    except ToolError as exc:
        if "not found" in str(exc):
            return None
        raise
    match = re.match(r"\S+ \(([^)\s]+)\)", text)
    return match.group(1) if match else None


async def checked_ref(http: ReadOnlyHTTP, package: str, ref: str) -> tuple[str, str]:
    """``ref`` as a git ref, and a note if a patches-applied ref had to be replaced.

    git-ubuntu stops updating a package's ``applied/`` branches when its quilt
    patches fail to apply once (samba's ``applied/ubuntu/devel`` is 3.6.3, and
    it has no ``applied/ubuntu/noble-devel``). Such a ref is replaced by the
    same tree without the patches applied, and the note says so.
    """
    git_ref = resolve_ref(ref)
    if not git_ref.startswith("applied/"):
        return git_ref, ""
    plain = unapplied_ref(git_ref)
    applied_version = await _head_version(http, package, git_ref)
    plain_version = await _head_version(http, package, plain)
    if plain_version is None or applied_version == plain_version:
        return git_ref, ""
    stale = f"is at {applied_version}" if applied_version else "does not exist"
    return plain, (
        f"git-ubuntu's {git_ref} {stale} but the package is at {plain_version}, so this is "
        f"{plain}, WITHOUT the Debian/Ubuntu patches applied"
    )


def _package(package: str) -> str:
    if not _PACKAGE.match(package):
        raise ToolError(f"{package!r} is not a source package name")
    return package


def _checked(resp: Any, package: str, what: str) -> str:
    if not resp.url.startswith(f"{CGIT}/"):
        # cgit sends unknown repositories to the Launchpad login page.
        raise ToolError(f"{package} is not imported in git-ubuntu (check the source package name)")
    if resp.status == 404:
        raise ToolError(
            f"{what} not found in git-ubuntu {package} (check the path and the version or branch)"
        )
    if resp.status >= 400:
        raise ToolError(f"git-ubuntu answered HTTP {resp.status} for {what}")
    text = resp.text()
    if resp.truncated:
        text += "\n[truncated]"
    return text


async def read_file(http: ReadOnlyHTTP, package: str, path: str, ref: str) -> str:
    """The content of ``path`` in ``package`` at ``ref`` (see :func:`resolve_ref`)."""
    git_ref = resolve_ref(ref)
    url = f"{CGIT}/{_package(package)}/plain/{quote(path.strip().lstrip('/'))}?h={quote(git_ref, safe='/')}"
    return _checked(await http.get(url), package, f"{path} at {git_ref}")


async def read_diff(http: ReadOnlyHTTP, package: str, old: str, new: str) -> str:
    """The diff between two versions (or refs) of ``package``."""
    old_ref, new_ref = resolve_ref(old), resolve_ref(new)
    url = f"{CGIT}/{_package(package)}/rawdiff/?id={quote(new_ref, safe='/')}&id2={quote(old_ref, safe='/')}"
    return _checked(await http.get(url), package, f"the diff {old_ref}..{new_ref}")


def parse_changelog(text: str) -> list[ChangeBlock]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return list(Changelog(text, strict=False))


def lp_bugs_closed(block: ChangeBlock) -> set[str]:
    """Launchpad bug numbers an entry closes (``LP: #123, #456``)."""
    found: set[str] = set()
    for match in _LP_BUGS.finditer(str(block)):
        found.update(re.findall(r"\d+", match.group(1)))
    return found


def _entry(block: ChangeBlock) -> str:
    lines = str(block).strip("\n").splitlines()
    if len(lines) > MAX_ENTRY_LINES:
        lines = [
            *lines[: MAX_ENTRY_LINES - 1],
            f"  [{len(lines) - MAX_ENTRY_LINES + 1} more lines]",
            lines[-1],
        ]
    return "\n".join(lines)


async def source_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    package, path = str_arg(args, "package"), str_arg(args, "path")
    http = need_http(ctx)
    git_ref, note = await checked_ref(http, package, str_arg(args, "ref", required=False))
    text = await read_file(http, package, path, git_ref)
    page = page_text(text, pattern=args.get("pattern"), offset=int_arg(args, "offset"))
    return f"[{note}]\n{page}" if note else page


async def pkg_changelog(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = str_arg(args, "package")
    ref = str_arg(args, "ref", required=False)
    since = str_arg(args, "since", required=False)
    pattern = str_arg(args, "pattern", required=False)
    blocks = parse_changelog(await read_file(need_http(ctx), package, "debian/changelog", ref))
    if since:
        try:
            floor = Version(since)
        except ValueError:
            raise ToolError(f"since must be a version, got {since!r}") from None
        blocks = [b for b in blocks if b.version is not None and b.version > floor]
    if pattern:
        try:
            regex = re.compile(pattern, re.I)
        except re.error as exc:
            raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc
        matching = [b for b in blocks if regex.search(str(b))]
        if not matching:
            return f"No entry of {len(blocks)} matches {pattern!r}."
        shown = matching[:MAX_CHANGELOG_MATCHES]
        note = f"[{len(matching)} of {len(blocks)} entries match; showing {len(shown)}]"
    else:
        shown = blocks[:CHANGELOG_ENTRIES]
        note = f"[newest {len(shown)} of {len(blocks)} entries; use since or pattern for others]"
    return "\n\n".join([note, *(_entry(b) for b in shown)])


async def source_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = _package(str_arg(args, "package"))
    directory = str_arg(args, "path", required=False).strip("/")
    http = need_http(ctx)
    git_ref, note = await checked_ref(http, package, str_arg(args, "ref", required=False))
    path = quote(directory + "/") if directory else ""
    url = f"{CGIT}/{package}/plain/{path}?h={quote(git_ref, safe='/')}"
    resp = await http.get(url)
    where = f"{directory or '/'} at {git_ref}"
    text = _checked(resp, package, where)
    if not resp.content_type.startswith("text/html"):
        raise ToolError(f"{directory} is a file, not a directory: read it with source_file")
    entries = [
        name + ("/" if href.endswith("/") else "")
        for href, name in _LISTING.findall(text)
        if name not in ("../", "..")
    ]
    if not entries:
        return f"{where} is empty."
    head = f"[{note}]\n" if note else ""
    return head + f"{len(entries)} entries in {where} (directories end with /):\n" + "\n".join(entries)


async def source_symbols(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = _package(str_arg(args, "package"))
    symbol = str_arg(args, "symbol")
    if not re.match(r"^[\w:.~$]+$", symbol):
        raise ToolError(f"symbol must be an identifier, got {symbol!r}")
    resp = await need_http(ctx).get(f"{SOURCES_API}?{urlencode({'ctag': symbol, 'package': package})}")
    if resp.status >= 400:
        raise ToolError(f"sources.debian.org answered HTTP {resp.status}")
    try:
        results = json.loads(resp.body).get("results") or []
    except ValueError:
        raise ToolError("sources.debian.org did not return JSON") from None
    # Patches' backups (.pc/) duplicate the real files.
    results = [r for r in results if not str(r.get("path", "")).startswith(".pc/")]
    if not results:
        return f"No definition of {symbol} in the Debian sources of {package}."

    def newest(result: dict[str, Any]) -> Version:
        try:
            return Version(str(result.get("version")))
        except ValueError:
            return Version("0")

    results.sort(key=newest, reverse=True)
    lines = [f"{r.get('path')}:{r.get('line')} (Debian {r.get('version')})" for r in results[:MAX_SYMBOLS]]
    return (
        f"Definitions of {symbol} in Debian's {package}, newest version first (line numbers may "
        "differ in Ubuntu; read the file with source_file and a pattern):\n" + "\n".join(lines)
    )


def _split_diff(diff: str) -> list[tuple[str, str]]:
    starts = [(m.start(), m.group(2)) for m in _DIFF_FILE.finditer(diff)]
    return [
        (path, diff[start : starts[i + 1][0] if i + 1 < len(starts) else len(diff)])
        for i, (start, path) in enumerate(starts)
    ]


def _stat(text: str) -> str:
    lines = text.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return f"+{added} -{removed}"


async def source_diff(ctx: ToolContext, args: dict[str, Any]) -> str:
    package = str_arg(args, "package")
    old, new = str_arg(args, "from"), str_arg(args, "to")
    globs = [g.strip() for g in str_arg(args, "path_glob", required=False).split(",") if g.strip()]
    files = _split_diff(await read_diff(need_http(ctx), package, old, new))
    if globs:
        files = [(path, text) for path, text in files if any(fnmatch.fnmatch(path, g) for g in globs)]
    if not files:
        return f"No changes between {old} and {new}" + (f" in {', '.join(globs)}." if globs else ".")
    parts = [f"{len(files)} files changed between {old} and {new}:"]
    parts += [f"  {path} ({_stat(text)})" for path, text in files]
    budget = MAX_DIFF
    omitted: list[str] = []
    for path, text in files:
        if len(text) <= budget:
            parts.append(text.rstrip("\n"))
            budget -= len(text)
        else:
            omitted.append(path)
    if omitted:
        parts.append(f"[diff of {len(omitted)} files omitted for size; narrow with path_glob]")
    return "\n".join(parts)


class UpstreamInfo(BaseModel):
    """Where a package comes from, as its packaging says."""

    homepage: str = ""
    repository: str = ""
    bug_database: str = ""
    watch: str = ""
    debian_vcs: str = ""
    # Searchable trackers: "github owner/repo", "gitlab host project".
    forges: list[str] = []


def forge_of(url: str) -> str:
    """``github owner/repo`` or ``gitlab host project`` for a forge URL, else ``""``."""
    if match := _GITHUB.match(url):
        return f"github {match.group(1)}/{match.group(2)}"
    parts = urlsplit(url)
    if parts.hostname in GITLAB_HOSTS:
        project = parts.path.split("/-/", 1)[0].strip("/").removesuffix(".git")
        if project.count("/") >= 1:
            return f"gitlab {parts.hostname} {project}"
    return ""


async def _optional(http: ReadOnlyHTTP, package: str, path: str) -> str:
    try:
        return await read_file(http, package, path, "devel")
    except ToolError as exc:
        if "not found" in str(exc):
            return ""
        raise


async def read_upstream_info(http: ReadOnlyHTTP, package: str) -> UpstreamInfo:
    """Upstream links from ``debian/control``, ``debian/upstream/metadata`` and ``debian/watch``."""
    control = Deb822(await read_file(http, package, "debian/control", "devel"))
    info = UpstreamInfo(
        homepage=control.get("Homepage", ""),
        debian_vcs=control.get("Vcs-Browser", "") or control.get("Vcs-Git", ""),
    )
    try:
        metadata = yaml.safe_load(await _optional(http, package, "debian/upstream/metadata")) or {}
    except yaml.YAMLError:
        metadata = {}
    if isinstance(metadata, dict):
        info.repository = str(metadata.get("Repository-Browse") or metadata.get("Repository") or "")
        info.bug_database = str(metadata.get("Bug-Database") or "")
    watch = await _optional(http, package, "debian/watch")
    if match := re.search(r"https?://[^\s\"']+", watch):
        info.watch = match.group(0)
    for url in (info.repository, info.bug_database, info.homepage, info.watch):
        forge = forge_of(url)
        if forge and forge not in info.forges:
            info.forges.append(forge)
    return info


async def upstream_info(ctx: ToolContext, args: dict[str, Any]) -> str:
    info = await read_upstream_info(need_http(ctx), str_arg(args, "package"))
    return json.dumps(info.model_dump(exclude_defaults=True), indent=1)


_PACKAGE_PARAM = {"type": "string", "description": "Source package name"}
_REF_PARAM = {
    "type": "string",
    "description": (
        "A version (e.g. 1.2-3ubuntu1), a series name (e.g. noble: its newest upload), 'devel' "
        "(default), 'debian' (Debian unstable), or a git-ubuntu branch (ubuntu/noble-updates, "
        "applied/ubuntu/noble-updates for patches applied)"
    ),
}
_PATTERN_PARAM = {"type": "string", "description": "Regex; return only matching lines"}

TOOLS = [
    ToolDef(
        name="pkg_changelog",
        group="source",
        risk=Risk.read,
        default=True,
        summary="Read the debian/changelog of an Ubuntu or Debian package version",
        description=(
            "Read debian/changelog of a source package at a version, series or branch (git-ubuntu). "
            "Returns the newest entries, the entries newer than 'since', or the entries matching the "
            "case-insensitive regex 'pattern' (e.g. 'LP: #2151788', a CVE, a function name)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "ref": _REF_PARAM,
                "since": {"type": "string", "description": "Only entries newer than this version"},
                "pattern": {"type": "string", "description": "Regex; return only matching entries"},
            },
            "required": ["package"],
        },
        run=pkg_changelog,
    ),
    ToolDef(
        name="source_file",
        group="source",
        risk=Risk.read,
        default=True,
        summary="Read one file of a package's source (git-ubuntu, no download)",
        description=(
            "Read one file of a source package at a version, series or branch (git-ubuntu). Use "
            "an applied/ ref to see the source with the Ubuntu/Debian patches applied. Returns a page "
            "of lines from 'offset', or the lines matching 'pattern'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "path": {"type": "string", "description": "Path in the source tree, e.g. debian/control"},
                "ref": _REF_PARAM,
                "pattern": _PATTERN_PARAM,
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
            },
            "required": ["package", "path"],
        },
        run=source_file,
    ),
    ToolDef(
        name="source_list",
        group="source",
        risk=Risk.read,
        default=True,
        summary="List a directory of a package's source (git-ubuntu, no download)",
        description=(
            "List the files and subdirectories of a directory in a source package at a version, "
            "series or branch (git-ubuntu). An empty path lists the top of the tree."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "path": {"type": "string", "description": "Directory, e.g. src/core (default: top)"},
                "ref": _REF_PARAM,
            },
            "required": ["package"],
        },
        run=source_list,
    ),
    ToolDef(
        name="source_symbols",
        group="source",
        risk=Risk.read,
        default=True,
        summary="Find where a function or symbol is defined (sources.debian.org)",
        description=(
            "Find the files and lines defining a function, type or variable in a package's Debian "
            "sources (sources.debian.org ctags), newest Debian version first. Then read the file "
            "with source_file and a pattern, as Ubuntu line numbers may differ."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "symbol": {"type": "string", "description": "Identifier, e.g. ngx_http_parse_request_line"},
            },
            "required": ["package", "symbol"],
        },
        run=source_symbols,
    ),
    ToolDef(
        name="source_diff",
        group="source",
        risk=Risk.read,
        default=True,
        summary="Diff two versions of a source package (git-ubuntu)",
        description=(
            "Show what changed between two versions (or refs) of a source package: the changed "
            "files with line counts, then the diff. Narrow large diffs with path_glob "
            "(e.g. 'debian/patches/*,src/*.c')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": _PACKAGE_PARAM,
                "from": {"type": "string", "description": "Older version or ref"},
                "to": {"type": "string", "description": "Newer version or ref"},
                "path_glob": {"type": "string", "description": "Comma-separated globs of paths to keep"},
            },
            "required": ["package", "from", "to"],
        },
        run=source_diff,
    ),
    ToolDef(
        name="upstream_info",
        group="source",
        risk=Risk.read,
        default=True,
        summary="Find a package's upstream homepage, repository and bug tracker",
        description=(
            "Upstream links of a source package from its packaging: Homepage, the upstream "
            "repository and bug database (debian/upstream/metadata), the debian/watch URL, the "
            "Debian packaging repository, and the trackers the search tools can query."
        ),
        parameters={
            "type": "object",
            "properties": {"package": _PACKAGE_PARAM},
            "required": ["package"],
        },
        run=upstream_info,
    ),
]
