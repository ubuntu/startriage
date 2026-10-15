"""Recorded HTTP for offline tool tests.

:class:`Recorder` wraps a real aiohttp session and keeps every exchange;
:class:`ReplaySession` serves them back to :class:`ReadOnlyHTTP`. Fixtures live
in ``tests/fixtures/ai/<name>.json``. To re-record one, run the matching
``record_*`` function in ``tests/record_ai_fixtures.py`` with network access.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "ai"


def _key(method: str, url: str, data: bytes | None) -> str:
    digest = hashlib.sha256(data).hexdigest()[:16] if data else ""
    return f"{method} {url} {digest}"


class _Content:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Response:
    def __init__(self, entry: dict[str, Any]):
        self.status = entry["status"]
        self.headers = {"Location": entry["location"]} if entry.get("location") else {}
        self.content_type = entry["content_type"]
        self.charset = entry.get("charset")
        self.content = _Content(entry["body"].encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ReplaySession:
    """Serves the exchanges of a fixture; an unrecorded request fails the test."""

    def __init__(self, name: str):
        entries = json.loads((FIXTURES / f"{name}.json").read_text())
        self._entries = {entry["key"]: entry for entry in entries}
        self.requests: list[str] = []

    def request(self, method: str, url: str, *, data: bytes | None = None, **_: Any) -> _Response:
        key = _key(method, url, data)
        self.requests.append(key)
        if key not in self._entries:
            raise AssertionError(f"unrecorded request: {key}")
        return _Response(self._entries[key])


class Recorder:
    """An aiohttp session stand-in that performs real requests and keeps them."""

    def __init__(self, session: Any):
        self._session = session
        self.entries: dict[str, dict[str, Any]] = {}

    def request(self, method: str, url: str, *, data: bytes | None = None, **kwargs: Any) -> Any:
        recorder = self

        class _Pending:
            async def __aenter__(self):
                async with recorder._session.request(method, url, data=data, **kwargs) as resp:
                    body = await resp.read()
                    entry = {
                        "key": _key(method, url, data),
                        "status": resp.status,
                        "content_type": resp.content_type,
                        "charset": resp.charset,
                        "location": resp.headers.get("Location"),
                        "body": body.decode(resp.charset or "utf-8", errors="replace"),
                    }
                recorder.entries[entry["key"]] = entry
                return _Response(entry)

            async def __aexit__(self, *exc):
                return False

        return _Pending()

    def save(self, name: str) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path = FIXTURES / f"{name}.json"
        path.write_text(json.dumps(list(self.entries.values()), ensure_ascii=False, indent=0) + "\n")
