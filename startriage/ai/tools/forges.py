"""Upstream and other-distribution trackers: GitHub, GitLab, Bugzilla, patches.

All anonymous reads. GitHub's search API allows 10 anonymous queries a
minute; a token (``github_token`` in the configuration, or ``GH_TOKEN``) raises
that and is sent only to ``api.github.com``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .base import Risk, ToolContext, ToolDef, ToolError, int_arg, need_http, page_text, str_arg
from .http import ReadOnlyHTTP
from .source import GITLAB_HOSTS

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# Bugzilla REST endpoints that answer anonymous searches.
BUGZILLAS = {
    "redhat": "https://bugzilla.redhat.com/rest/bug",
    "gentoo": "https://bugs.gentoo.org/rest/bug",
    "samba": "https://bugzilla.samba.org/rest.cgi/bug",
    "mozilla": "https://bugzilla.mozilla.org/rest/bug",
    "kernel": "https://bugzilla.kernel.org/rest/bug",
}
MAX_RESULTS = 20
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_PROJECT = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")
_SHA = r"[0-9a-f]{7,40}"


def _json(resp: Any, what: str) -> Any:
    if resp.status >= 400:
        raise ToolError(f"{what} answered HTTP {resp.status}")
    try:
        return json.loads(resp.body)
    except ValueError:
        raise ToolError(f"{what} did not return JSON") from None


def _issue(item: dict[str, Any]) -> dict[str, Any]:
    pr = item.get("pull_request")
    entry = {
        "number": item.get("number"),
        "kind": "pull request" if pr else "issue",
        "title": item.get("title"),
        "state": item.get("state"),
        "merged": bool(pr.get("merged_at")) if pr else None,
        "url": item.get("html_url"),
        "created": str(item.get("created_at") or "")[:10],
        "updated": str(item.get("updated_at") or "")[:10],
        "closed": str(item.get("closed_at") or "")[:10],
        "comments": item.get("comments"),
        "labels": [label.get("name") for label in item.get("labels") or []],
    }
    return {key: value for key, value in entry.items() if value not in (None, "", [])}


def _commit(item: dict[str, Any]) -> dict[str, Any]:
    commit = item.get("commit") or {}
    return {
        "sha": str(item.get("sha") or "")[:12],
        "title": str(commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
        "date": str((commit.get("committer") or {}).get("date") or "")[:10],
        "url": item.get("html_url"),
    }


async def search_github(http: ReadOnlyHTTP, repo: str, query: str, kind: str) -> list[dict[str, Any]]:
    if not _REPO.match(repo):
        raise ToolError(f"repo must be owner/name, got {repo!r}")
    endpoint = "commits" if kind == "commits" else "issues"
    params = urlencode({"q": f"repo:{repo} {query}", "per_page": MAX_RESULTS})
    resp = await http.get(f"{GITHUB_API}/search/{endpoint}?{params}", headers=GITHUB_HEADERS)
    if resp.status in (403, 429):
        raise ToolError("GitHub rate limit reached; wait, or set github_token in the configuration")
    if resp.status == 422:
        raise ToolError(f"GitHub cannot search {repo} (does the repository exist?)")
    items = _json(resp, "GitHub").get("items") or []
    return [_commit(item) if endpoint == "commits" else _issue(item) for item in items]


async def github_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    repo, query = str_arg(args, "repo"), str_arg(args, "query")
    kind = str_arg(args, "kind", required=False) or "issues"
    found = await search_github(need_http(ctx), repo, query, kind)
    if not found:
        return f"Nothing in {repo} matches {query!r}."
    return json.dumps(found, ensure_ascii=False, indent=1)


async def search_gitlab(
    http: ReadOnlyHTTP, host: str, project: str, query: str, kind: str
) -> list[dict[str, Any]]:
    if host not in GITLAB_HOSTS:
        raise ToolError(f"unknown GitLab host {host!r}; known: {', '.join(sorted(GITLAB_HOSTS))}")
    if not _PROJECT.match(project):
        raise ToolError(f"project must be a path like group/name, got {project!r}")
    endpoint = "merge_requests" if kind == "merge_requests" else "issues"
    params = urlencode({"search": query, "state": "all", "per_page": MAX_RESULTS})
    url = f"https://{host}/api/v4/projects/{quote(project, safe='')}/{endpoint}?{params}"
    resp = await http.get(url)
    if resp.status == 404:
        raise ToolError(f"{host} has no public project {project!r} (or its {endpoint} are disabled)")
    found = []
    for item in _json(resp, host):
        entry = {
            "number": item.get("iid"),
            "title": item.get("title"),
            "state": item.get("state"),
            "url": item.get("web_url"),
            "created": str(item.get("created_at") or "")[:10],
            "updated": str(item.get("updated_at") or "")[:10],
            "comments": item.get("user_notes_count"),
            "labels": item.get("labels") or [],
        }
        found.append({key: value for key, value in entry.items() if value not in (None, "", [])})
    return found


async def gitlab_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    host, project, query = str_arg(args, "host"), str_arg(args, "project"), str_arg(args, "query")
    kind = str_arg(args, "kind", required=False) or "issues"
    found = await search_gitlab(need_http(ctx), host, project, query, kind)
    if not found:
        return f"Nothing in {host}/{project} matches {query!r}."
    return json.dumps(found, ensure_ascii=False, indent=1)


async def search_bugzilla(
    http: ReadOnlyHTTP, instance: str, query: str, *, include_closed: bool = True
) -> list[dict[str, Any]]:
    if instance not in BUGZILLAS:
        raise ToolError(f"unknown Bugzilla {instance!r}; known: {', '.join(BUGZILLAS)}")
    base = BUGZILLAS[instance]
    params = urlencode(
        {
            "quicksearch": f"ALL {query}" if include_closed else query,
            "include_fields": "id,summary,status,resolution,product,component,last_change_time",
            "limit": MAX_RESULTS,
        }
    )
    bugs = _json(await http.get(f"{base}?{params}"), f"{instance} Bugzilla").get("bugs") or []
    # Some instances ignore limit; the newest are the most useful.
    bugs = sorted(bugs, key=lambda b: str(b.get("last_change_time")), reverse=True)[:MAX_RESULTS]
    show = base.rsplit("/", 2)[0] + "/show_bug.cgi?id={}"
    return [
        {
            "id": bug.get("id"),
            "summary": bug.get("summary"),
            "status": " ".join(filter(None, [bug.get("status"), bug.get("resolution")])),
            "product": f"{bug.get('product')} / {bug.get('component')}",
            "updated": str(bug.get("last_change_time") or "")[:10],
            "url": show.format(bug.get("id")),
        }
        for bug in bugs
    ]


async def bugzilla_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    instance, query = str_arg(args, "instance"), str_arg(args, "query")
    include_closed = bool(args.get("include_closed", True))
    found = await search_bugzilla(need_http(ctx), instance, query, include_closed=include_closed)
    if not found:
        return f"No {instance} bug matches {query!r}."
    return json.dumps(found, ensure_ascii=False, indent=1)


def patch_url(url: str) -> str:
    """The URL of the raw patch behind a commit, pull/merge request or patch link."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith((".patch", ".diff", ".debdiff")):
        return url
    host = parts.hostname or ""
    if host == "github.com" and re.search(rf"/commit/{_SHA}$|/pull/\d+$", path):
        return f"https://github.com{path}.patch"
    if re.search(rf"/-/commit/{_SHA}$|/-/merge_requests/\d+$", path):
        return f"https://{host}{path}.patch"
    if path.endswith("/commit") and "id=" in parts.query:
        return url.replace("/commit/?", "/patch/?").replace("/commit?", "/patch?")
    if re.search(r"(^|;|&)a=commit(diff)?(;|&|$)", parts.query):
        return re.sub(r"a=commit(diff)?", "a=patch", url)
    attachment = re.search(r"(?:^|&)id=(\d+)(?:&|$)", parts.query)
    if path.endswith("/attachment.cgi") and attachment:
        return f"https://{host}{path}?id={attachment.group(1)}"
    raise ToolError(
        "not a recognised commit or patch URL (GitHub/GitLab commit, pull or merge request, cgit, "
        "gitweb, a Bugzilla attachment, or a .patch/.diff link)"
    )


async def fetch_patch(ctx: ToolContext, args: dict[str, Any]) -> str:
    url = patch_url(str_arg(args, "url"))
    resp = await need_http(ctx).get(url)
    if resp.status >= 400:
        raise ToolError(f"HTTP {resp.status} for {url}")
    if not (resp.content_type.startswith("text/") or resp.content_type in ("", "application/octet-stream")):
        raise ToolError(f"{url} is not a patch ({resp.content_type})")
    text = resp.text()
    if resp.truncated:
        text += "\n[patch truncated]"
    return page_text(text, pattern=args.get("pattern"), offset=int_arg(args, "offset"))


_QUERY_PARAM = {"type": "string", "description": "Words to search for (an error message, a function)"}

TOOLS = [
    ToolDef(
        name="github_search",
        group="forges",
        risk=Risk.read,
        default=True,
        summary="Search a GitHub repository's issues, pull requests or commits",
        description=(
            "Search one GitHub repository. kind 'issues' (default) finds issues and pull requests "
            "(with state and whether a PR was merged); 'commits' searches commit messages. The query "
            "uses GitHub search syntax (e.g. 'segfault in:title', 'is:pr is:merged crash')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name, e.g. nginx/nginx"},
                "query": _QUERY_PARAM,
                "kind": {"type": "string", "enum": ["issues", "commits"]},
            },
            "required": ["repo", "query"],
        },
        run=github_search,
    ),
    ToolDef(
        name="gitlab_search",
        group="forges",
        risk=Risk.read,
        default=True,
        summary="Search a GitLab project's issues or merge requests (salsa, freedesktop, ...)",
        description=(
            "Search the issues (default) or merge requests of a project on a known GitLab instance: "
            + ", ".join(sorted(GITLAB_HOSTS))
            + ". Commits cannot be searched."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "e.g. gitlab.freedesktop.org"},
                "project": {"type": "string", "description": "Project path, e.g. mesa/mesa"},
                "query": _QUERY_PARAM,
                "kind": {"type": "string", "enum": ["issues", "merge_requests"]},
            },
            "required": ["host", "project", "query"],
        },
        run=gitlab_search,
    ),
    ToolDef(
        name="bugzilla_search",
        group="forges",
        risk=Risk.read,
        default=True,
        summary="Search a Bugzilla (Red Hat/Fedora, Gentoo, Samba, Mozilla, kernel)",
        description=(
            "Quick-search a Bugzilla instance, closed bugs included: "
            + ", ".join(BUGZILLAS)
            + ". The query is Bugzilla quicksearch syntax: words, or e.g. 'product:Fedora nginx crash'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "instance": {"type": "string", "enum": list(BUGZILLAS)},
                "query": _QUERY_PARAM,
                "include_closed": {"type": "boolean", "description": "Default true"},
            },
            "required": ["instance", "query"],
        },
        run=bugzilla_search,
    ),
    ToolDef(
        name="fetch_patch",
        group="forges",
        risk=Risk.read,
        default=True,
        summary="Read the patch behind a commit, pull/merge request or patch URL",
        description=(
            "Read the raw patch of a GitHub or GitLab commit, pull request or merge request, a cgit "
            "or gitweb commit, or a .patch/.diff/.debdiff link. Returns a page of lines from "
            "'offset', or the lines matching 'pattern'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Commit, pull request or patch URL"},
                "pattern": {"type": "string", "description": "Regex; return only matching lines"},
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
            },
            "required": ["url"],
        },
        run=fetch_patch,
    ),
]
