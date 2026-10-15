"""Re-record the HTTP fixtures of tests/test_ai_sources.py (needs network).

    uv run python -m tests.record_ai_fixtures [NAME...]

The calls below must stay in sync with the tests that replay them. Without
names, every fixture is recorded again.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

from startriage.ai.search import changelog_hits, previous_version, search_debian
from startriage.ai.tools import ReadOnlyHTTP, ToolContext, build_tools, call_tool, debian

from .http_replay import Recorder

NOBLE_SG3 = {"noble": {"published": {"Release": "1.46-3ubuntu4", "Updates": "1.46-3ubuntu4.1"}}}
NGINX_FIX = "https://github.com/nginx/nginx/commit/2046b45aa0c6e712c216b9075886f3f26e9b4ca9"
# Samba BZ 15979 (LP #2080070): the printing leak fixes, backported to 4.22-4.24.
SAMBA_FIX = "https://bugzilla.samba.org/attachment.cgi?id=18821"
# A new file (applies anywhere) and a change to a file nginx does not have.
NEW_AND_MISSING = """\
--- /dev/null
+++ b/debian/patches/lp-fix.patch
@@ -0,0 +1,2 @@
+Description: a fix
+Author: someone
--- a/src/nope.c
+++ b/src/nope.c
@@ -1 +1 @@
-old
+new
"""

TOOL_CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "sg3-utils": [
        ("debian_bugs", {"package": "sg3-utils", "text": "rules missing"}),
        ("debian_bug", {"bug": "1145692"}),
        ("pkg_changelog", {"package": "sg3-utils", "ref": "noble", "pattern": "LP: #"}),
        ("upstream_info", {"package": "sg3-utils"}),
    ],
    "nginx": [
        ("source_diff", {"package": "nginx", "from": "1.24.0-2ubuntu7.17", "to": "1.24.0-2ubuntu7.18"}),
        ("github_search", {"repo": "nginx/nginx", "query": "buffer overrun", "kind": "commits"}),
        (
            "fetch_patch",
            {"url": "https://github.com/nginx/nginx/commit/2046b45aa0c6e712c216b9075886f3f26e9b4ca9"},
        ),
        ("source_file", {"package": "no-such-package-xyz", "path": "debian/control"}),
        ("upstream_info", {"package": "nginx"}),
    ],
    "forges": [
        (
            "gitlab_search",
            {
                "host": "gitlab.archlinux.org",
                "project": "archlinux/packaging/packages/samba",
                "query": "winbind",
            },
        ),
        ("bugzilla_search", {"instance": "redhat", "query": "samba winbind crash"}),
    ],
    "source": [
        ("source_list", {"package": "nginx", "ref": "noble", "path": "src/http/v2"}),
        ("source_list", {"package": "nginx", "ref": "noble", "path": "README"}),
        ("source_symbols", {"package": "nginx", "symbol": "ngx_http_script_regex_end_code"}),
        (
            "patch_applies",
            {"package": "nginx", "url": NGINX_FIX, "ref": "noble,devel,1.18.0-6ubuntu14"},
        ),
        ("patch_applies", {"package": "nginx", "patch": NEW_AND_MISSING, "ref": "noble"}),
        ("source_file", {"package": "nginx", "path": "src/http/ngx_http_script.c", "ref": "noble"}),
        # samba's applied/ branches are stale (devel) or missing (noble): checked without patches.
        ("patch_applies", {"package": "samba", "url": SAMBA_FIX, "ref": "noble,devel"}),
    ],
}


async def run_calls(http: ReadOnlyHTTP, name: str) -> dict[str, str]:
    """Each tool call of fixture ``name``, by position, and its output."""
    out = {}
    for index, (tool, args) in enumerate(TOOL_CALLS[name]):
        spec = build_tools(ToolContext(http=http), [tool])[0]
        text, ok = await call_tool(spec, args)
        out[f"{index}:{tool}"] = f"ok={ok}\n{text}"
    if name == "sg3-utils":
        found = await search_debian(http, ["sg3-utils"], ["rules missing", "rescan-scsi-bus.sh"])
        out["search_debian"] = "\n".join(f"{c.ref} {c.title} {c.fixed_version}" for c in found)
        hits = await changelog_hits(http, "sg3-utils", ["2039279", "1976228", "2137191"], NOBLE_SG3)
        out["changelog_hits"] = "\n".join(repr(h) for h in hits)
        out["previous_version"] = await previous_version(http, "sg3-utils", "1.46-3ubuntu4.1")
    return out


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        for name in sys.argv[1:] or TOOL_CALLS:
            recorder = Recorder(session)
            http = ReadOnlyHTTP(recorder, allowed_posts=debian.ALLOWED_POSTS)  # ty: ignore[invalid-argument-type]
            for key, text in (await run_calls(http, name)).items():
                print(f"===== {name} {key}\n{text[:1500]}\n")
            recorder.save(name)


if __name__ == "__main__":
    asyncio.run(main())
