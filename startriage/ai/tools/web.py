"""Web read tools: check that a URL exists, read a web page."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from .base import Risk, ToolContext, ToolDef, ToolError, page_text
from .http import ReadOnlyHTTP


def _http(ctx: ToolContext) -> ReadOnlyHTTP:
    if ctx.http is None:
        raise ToolError("HTTP is not available in this run")
    return ctx.http


async def url_status(http: ReadOnlyHTTP, url: str) -> tuple[int, str]:
    """Return the HTTP status and final URL of ``url`` (HEAD, falling back to a short GET)."""
    resp = await http.head(url)
    if resp.status >= 400:
        # Some servers refuse HEAD; ask for the start of the body instead.
        resp = await http.get(url, max_body=65536)
    return resp.status, resp.url


async def verify_url(ctx: ToolContext, args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    status, final = await url_status(_http(ctx), url)
    verdict = "exists" if status < 400 else "does NOT resolve"
    return f"HTTP {status}: {verdict} (final URL: {final})"


class _TextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "svg"})
    _BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre", "table", "section"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


async def fetch_url(ctx: ToolContext, args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    try:
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError):
        raise ToolError("offset must be an integer") from None
    resp = await _http(ctx).get(url)
    if resp.status >= 400:
        raise ToolError(f"HTTP {resp.status} for {resp.url}")
    if not (resp.content_type.startswith("text/") or resp.content_type.endswith(("json", "xml"))):
        raise ToolError(f"{resp.url} is {resp.content_type}, not text")
    text = resp.text()
    if resp.content_type == "text/html":
        text = html_to_text(text)
    if resp.truncated:
        text += "\n[page truncated]"
    return page_text(text, pattern=args.get("pattern"), offset=offset)


_URL_PARAM = {"type": "string", "description": "http(s) URL"}

TOOLS = [
    ToolDef(
        name="verify_url",
        group="web",
        risk=Risk.read,
        default=True,
        summary="Check that a URL exists (HTTP status only, no content)",
        description=(
            "Check that a public http(s) URL resolves, e.g. before citing a commit, patch or issue. "
            "Returns only the HTTP status and final URL."
        ),
        parameters={"type": "object", "properties": {"url": _URL_PARAM}, "required": ["url"]},
        run=verify_url,
    ),
    ToolDef(
        name="fetch_url",
        group="web",
        risk=Risk.read,
        # Off by default: the model chooses what to send to arbitrary sites.
        default=False,
        summary="Read any public web page (the model chooses the URL)",
        description=(
            "Read a public http(s) page as text (HTML is converted). Returns a page of lines from "
            "'offset', or only the lines matching the case-insensitive regex 'pattern'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": _URL_PARAM,
                "pattern": {"type": "string", "description": "Regex; return only matching lines"},
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
            },
            "required": ["url"],
        },
        run=fetch_url,
    ),
]
