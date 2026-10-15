"""Read-only HTTP for the agent's tools.

The model chooses the URLs, so every request is checked here rather than
trusted: http(s) only, GET/HEAD only (plus explicitly allowed POST endpoints),
public addresses only, redirects re-checked hop by hop, bounded bodies, and
credentials sent only to the exact host they belong to.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp

from .base import ToolError

MAX_BODY = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT = aiohttp.ClientTimeout(total=30)
# GET responses kept per run (the same changelog or search is often read twice).
MAX_CACHED = 256


class NetworkError(ToolError):
    """The host could not be reached or did not answer in time."""


@dataclass
class HTTPResponse:
    status: int
    url: str
    content_type: str
    charset: str | None
    body: bytes
    truncated: bool

    def text(self) -> str:
        return self.body.decode(self.charset or "utf-8", errors="replace")


class ReadOnlyHTTP:
    """HTTP client that can only read from public hosts.

    ``allowed_posts`` lists exact URLs where a POST is a read (e.g. a SOAP query
    API). ``credentials`` maps a host name to headers sent only to that host
    over https. Successful GET responses are cached for the life of the client
    (one run).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        allowed_posts: frozenset[str] = frozenset(),
        credentials: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._session = session
        self._allowed_posts = allowed_posts
        self._credentials = credentials or {}
        self._cache: dict[tuple[str, int, tuple[tuple[str, str], ...]], HTTPResponse] = {}

    async def get(
        self, url: str, *, max_body: int = MAX_BODY, headers: dict[str, str] | None = None
    ) -> HTTPResponse:
        key = (url, max_body, tuple(sorted((headers or {}).items())))
        if key in self._cache:
            return self._cache[key]
        resp = await self._request("GET", url, max_body=max_body, headers=headers)
        # Not rate limits or server errors: those may pass on a retry.
        if resp.status < 400 or resp.status == 404:
            if len(self._cache) >= MAX_CACHED:
                del self._cache[next(iter(self._cache))]
            self._cache[key] = resp
        return resp

    async def head(self, url: str) -> HTTPResponse:
        return await self._request("HEAD", url, max_body=0)

    async def post(self, url: str, data: bytes, headers: dict[str, str]) -> HTTPResponse:
        if url not in self._allowed_posts:
            raise ToolError(f"POST to {url} is not allowed")
        return await self._request("POST", url, data=data, headers=headers, max_body=MAX_BODY)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        max_body: int,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResponse:
        try:
            return await self._follow(method, url, max_body=max_body, data=data, headers=headers)
        except (aiohttp.ClientError, TimeoutError) as exc:
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise NetworkError(f"{method} {url} failed: {detail}") from exc

    async def _follow(
        self,
        method: str,
        url: str,
        *,
        max_body: int,
        data: bytes | None,
        headers: dict[str, str] | None,
    ) -> HTTPResponse:
        for _ in range(MAX_REDIRECTS + 1):
            await check_public_url(url)
            all_headers = {**(headers or {}), **self._headers_for(url)}
            async with self._session.request(
                method, url, data=data, headers=all_headers, allow_redirects=False, timeout=TIMEOUT
            ) as resp:
                location = resp.headers.get("Location")
                if resp.status in (301, 302, 303, 307, 308) and location:
                    url = urljoin(url, location)
                    if resp.status == 303 or (resp.status in (301, 302) and method == "POST"):
                        method, data = "GET", None
                    continue
                body, truncated = await _read_bounded(resp, max_body) if method != "HEAD" else (b"", False)
                return HTTPResponse(
                    status=resp.status,
                    url=url,
                    content_type=resp.content_type,
                    charset=resp.charset,
                    body=body,
                    truncated=truncated,
                )
        raise ToolError(f"too many redirects (more than {MAX_REDIRECTS})")

    def _headers_for(self, url: str) -> dict[str, str]:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return {}
        return self._credentials.get(parts.hostname or "", {})


async def _read_bounded(resp: aiohttp.ClientResponse, max_body: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_chunked(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size > max_body:
            return b"".join(chunks)[:max_body], True
    return b"".join(chunks), False


async def check_public_url(url: str) -> None:
    """Raise :class:`ToolError` unless ``url`` is http(s) on a public address.

    Blocks requests to loopback, private, link-local (cloud metadata) and other
    non-global addresses, so a prompt-injected URL cannot reach the user's
    local network. (The name is resolved again on connect; DNS rebinding is out
    of scope.)
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError(f"only http(s) URLs are allowed, got {url!r}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise ToolError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not address.is_global:
            raise ToolError(f"{parts.hostname} resolves to a non-public address; not allowed")
