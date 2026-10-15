"""Debian bug tracker tools, over the BTS SOAP interface.

The SOAP calls are reads sent as POST, so :data:`ALLOWED_POSTS` lists the one
endpoint :class:`~startriage.ai.tools.http.ReadOnlyHTTP` may POST to. The BTS
has no full-text search over SOAP: bugs are listed per source package and
filtered by the words of their subject.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.parser import HeaderParser
from typing import Any
from xml.sax.saxutils import escape

from .base import Risk, ToolContext, ToolDef, ToolError, int_arg, need_http, page_text, parse_bug_arg, str_arg
from .http import ReadOnlyHTTP

SOAP_URL = "https://bugs.debian.org/cgi-bin/soap.cgi"
ALLOWED_POSTS = frozenset({SOAP_URL})
BUG_URL = "https://bugs.debian.org/{}"
# Newest bugs of a package whose status is read (big packages have thousands).
MAX_BUGS = 500
STATUS_BATCH = 100
MAX_RESULTS = 25
MAX_MESSAGE_CHARS = 4000
_STOPWORDS = frozenset(
    "the and for with not when from into after does doesn't cannot can't fails failed failure error "
    "on in of to is a an it at by be".split()
)
_ENVELOPE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
    'xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/">'
    '<soap:Body><ns:{method} xmlns:ns="Debbugs/SOAP">{params}</ns:{method}></soap:Body></soap:Envelope>'
)


def _array(name: str, values: list[tuple[str, str]]) -> str:
    items = "".join(f'<ns:item xsi:type="xsd:{kind}">{escape(value)}</ns:item>' for kind, value in values)
    return (
        f'<ns:{name} soapenc:arrayType="xsd:anyType[{len(values)}]" xsi:type="soapenc:Array">'
        f"{items}</ns:{name}>"
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(element: ET.Element) -> Any:
    """SOAP-encoded XML as Python: arrays as lists, structs and maps as dicts."""
    children = list(element)
    if not children:
        return element.text or ""
    if all(_local(child.tag) == "item" for child in children):
        items = [_parse(child) for child in children]
        if all(isinstance(item, dict) and set(item) == {"key", "value"} for item in items):
            return {str(item["key"]): item["value"] for item in items}
        return items
    return {_local(child.tag): _parse(child) for child in children}


async def soap_call(http: ReadOnlyHTTP, method: str, params: str) -> Any:
    """Call ``method`` and return its parsed result."""
    body = _ENVELOPE.format(method=method, params=params).encode()
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"Debbugs/SOAP"'}
    resp = await http.post(SOAP_URL, body, headers)
    if resp.status >= 400:
        raise ToolError(f"the Debian BTS answered HTTP {resp.status}")
    try:
        root = ET.fromstring(resp.body)
    except ET.ParseError as exc:
        raise ToolError(f"the Debian BTS returned malformed XML: {exc}") from None
    for element in root.iter():
        if _local(element.tag) == "Fault":
            raise ToolError(
                f"the Debian BTS refused the query: {ET.tostring(element, encoding='unicode')[:300]}"
            )
        if _local(element.tag) == f"{method}Response":
            children = list(element)
            return _parse(children[0]) if children else ""
    raise ToolError("the Debian BTS returned no result")


async def bug_numbers(http: ReadOnlyHTTP, package: str, *, archived: bool = True) -> list[int]:
    """Bug numbers of a source package, newest first (archived ones too if asked)."""
    query = [("string", "src"), ("string", package)]
    if archived:
        query += [("string", "archive"), ("string", "both")]
    result = await soap_call(http, "get_bugs", _array("query", query))
    numbers = result if isinstance(result, list) else [result] if result else []
    return sorted((int(n) for n in numbers if str(n).isdigit()), reverse=True)


def _versions(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    return [str(v).rsplit("/", 1)[-1] for v in values if v]


def _date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), UTC).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _summary(number: str, raw: dict[str, Any]) -> dict[str, Any]:
    fixed = _versions(raw.get("fixed_versions"))
    done = raw.get("pending") == "done" or bool(raw.get("done"))
    status = "done" if done else "forwarded" if raw.get("forwarded") else "open"
    summary = {
        "bug": number,
        "url": BUG_URL.format(number),
        "subject": raw.get("subject") or "",
        "package": raw.get("package") or "",
        "severity": raw.get("severity") or "",
        "status": status,
        "found_in": _versions(raw.get("found_versions")),
        "fixed_in": fixed,
        "tags": str(raw.get("tags") or "").split(),
        "forwarded": raw.get("forwarded") or "",
        "merged_with": str(raw.get("mergedwith") or "").split(),
        "reported": _date(raw.get("date")),
        "last_modified": _date(raw.get("last_modified")),
    }
    return {key: value for key, value in summary.items() if value not in ("", [])}


async def bug_status(http: ReadOnlyHTTP, numbers: list[int]) -> list[dict[str, Any]]:
    """Summaries of ``numbers`` (in that order)."""
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(numbers), STATUS_BATCH):
        batch = numbers[start : start + STATUS_BATCH]
        result = await soap_call(http, "get_status", _array("bugs", [("int", str(n)) for n in batch]))
        if isinstance(result, dict):
            found.update({key: value for key, value in result.items() if isinstance(value, dict)})
    return [_summary(str(n), found[str(n)]) for n in numbers if str(n) in found]


async def package_bugs(http: ReadOnlyHTTP, package: str, *, archived: bool = True) -> list[dict[str, Any]]:
    """Summaries of the newest :data:`MAX_BUGS` bugs of a source package."""
    return await bug_status(http, (await bug_numbers(http, package, archived=archived))[:MAX_BUGS])


def words(text: str) -> list[str]:
    """The significant words of a search phrase, lowercase."""
    found = re.findall(r"[a-z0-9][a-z0-9_.+-]*[a-z0-9]|[a-z0-9]", text.lower())
    return [w for w in dict.fromkeys(found) if w not in _STOPWORDS and len(w) > 2]


def matches(subject: str, phrase: str) -> bool:
    """True if ``subject`` has the words of ``phrase`` (2 of 3 when it has more than two)."""
    wanted = words(phrase)
    if not wanted:
        return False
    have = set(words(subject)) | {w for w in wanted if w in subject.lower()}
    hits = sum(w in have for w in wanted)
    return hits == len(wanted) if len(wanted) <= 2 else hits * 3 >= len(wanted) * 2


async def debian_bugs(ctx: ToolContext, args: dict[str, Any]) -> str:
    package, text = str_arg(args, "package"), str_arg(args, "text", required=False)
    include_done = bool(args.get("include_done", True))
    bugs = await package_bugs(need_http(ctx), package, archived=include_done)
    if not include_done:
        bugs = [b for b in bugs if b["status"] != "done"]
    if text:
        bugs = [b for b in bugs if matches(b.get("subject", ""), text)]
    if not bugs:
        return f"No Debian bug of {package} matches {text!r}." if text else f"{package} has no Debian bugs."
    note = f"[{len(bugs)} bugs, newest first; showing {min(len(bugs), MAX_RESULTS)}]"
    return note + "\n" + json.dumps(bugs[:MAX_RESULTS], ensure_ascii=False, indent=1)


def _message(index: int, raw: dict[str, Any]) -> str:
    headers = HeaderParser().parsestr(str(raw.get("header") or ""))
    body = str(raw.get("body") or "")
    if len(body) > MAX_MESSAGE_CHARS:
        body = body[:MAX_MESSAGE_CHARS] + "\n[message truncated]"
    head = f"--- message {index}: from {headers.get('From', '?')}, {headers.get('Date', '?')}"
    if subject := headers.get("Subject"):
        head += f"\nSubject: {subject}"
    return f"{head}\n{body.strip()}"


async def debian_bug(ctx: ToolContext, args: dict[str, Any]) -> str:
    number = parse_bug_arg(args)
    http = need_http(ctx)
    status = await bug_status(http, [number])
    if not status:
        raise ToolError(f"Debian bug {number} does not exist")
    log = await soap_call(http, "get_bug_log", f'<ns:bugnumber xsi:type="xsd:int">{number}</ns:bugnumber>')
    messages = [m for m in (log if isinstance(log, list) else []) if isinstance(m, dict) and m.get("body")]
    text = "\n\n".join(
        [
            json.dumps(status[0], ensure_ascii=False, indent=1),
            *(_message(i, m) for i, m in enumerate(messages)),
        ]
    )
    return page_text(text, pattern=args.get("pattern"), offset=int_arg(args, "offset"))


TOOLS = [
    ToolDef(
        name="debian_bugs",
        group="debian",
        risk=Risk.read,
        default=True,
        summary="List the Debian bugs of a source package (filtered by subject words)",
        description=(
            "List the Debian bugs of a source package, newest first, with status, severity and the "
            "versions they were found and fixed in. 'text' keeps bugs whose subject has its words "
            "(the BTS has no full-text search: use 1-3 distinctive words)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Debian source package name"},
                "text": {"type": "string", "description": "Words the subject must contain"},
                "include_done": {"type": "boolean", "description": "Include closed bugs (default true)"},
            },
            "required": ["package"],
        },
        run=debian_bugs,
    ),
    ToolDef(
        name="debian_bug",
        group="debian",
        risk=Risk.read,
        default=True,
        summary="Read a Debian bug (status and messages)",
        description=(
            "Read a Debian bug: its status, then its messages. Returns a page of lines from "
            "'offset', or only the lines matching the case-insensitive regex 'pattern'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "bug": {"type": "integer", "description": "Debian bug number"},
                "pattern": {"type": "string", "description": "Regex; return only matching lines"},
                "offset": {"type": "integer", "description": "First line to return (0-based)"},
            },
            "required": ["bug"],
        },
        run=debian_bug,
    ),
]
