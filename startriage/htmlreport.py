"""Render triage and todo results as one self-contained html page with checkable rows."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from string import Template

from .output import Flag, Link, ReportItem, TriageResult
from .savebugs import BugPersistor
from .source import FetchMode

# the first matching rule picks the section of an item
_TODO_RULES: list[tuple[str, Callable[[ReportItem], bool]]] = [
    ("Bugs gone", lambda i: Flag.GONE in i.flags),
    ("Freezer", lambda i: Flag.FREEZER in i.flags),
    ("No owner", lambda i: not i.assignees),
    ("No recent updates", lambda i: Flag.OLD in i.flags),
    ("Recently marked todo", lambda i: Flag.NEW in i.flags),
    ("Needs verification", lambda i: Flag.VERIFICATION_NEEDED in i.flags),
    ("External updates", lambda i: Flag.EXTERNAL in i.flags),
    ("Other", lambda i: True),
]
_TODO_ORDER = [
    "Bugs gone",
    "No owner",
    "No recent updates",
    "Recently marked todo",
    "Needs verification",
    "External updates",
    "Freezer",
    "Other",
]
_TRIAGE_ORDER = ["launchpad", "github", "discourse", "proposed"]
_TRIAGE_TITLES = {"launchpad": "Launchpad", "github": "GitHub"}
# header of the context column, per source
_CONTEXT_LABELS = {"launchpad": "Package", "github": "Repo", "discourse": "Category", "proposed": "Versions"}

# flags shown as chips next to the title
_CHIPS = {
    Flag.NEW: "new",
    Flag.EXTERNAL: "reply pending",
    Flag.VERIFICATION_NEEDED: "verification needed",
    Flag.VERIFICATION_DONE: "verification done",
    Flag.EXPIRING: "expiring",
}


def todo_sections(items: list[ReportItem]) -> list[tuple[str, list[ReportItem]]]:
    """Put each item into the first matching housekeeping section, stalest first."""
    sections: dict[str, list[ReportItem]] = {name: [] for name in _TODO_ORDER}
    for item in items:
        name = next(name for name, match in _TODO_RULES if match(item))
        sections[name].append(item)
    for section in sections.values():
        section.sort(key=lambda i: (i.updated is None, i.updated or datetime.min))
    return list(sections.items())


def triage_sections(items: list[ReportItem]) -> list[tuple[str, list[ReportItem]]]:
    """One section per source."""
    sections: dict[str, list[ReportItem]] = {name: [] for name in _TRIAGE_ORDER}
    for item in items:
        sections.setdefault(item.source, []).append(item)
    return [(_TRIAGE_TITLES.get(name, name.capitalize()), section) for name, section in sections.items()]


async def write_report(
    path: Path,
    results: Mapping[str, TriageResult],
    mode: FetchMode,
    title: str,
    subtitle: str,
    persistor: BugPersistor | None = None,
    markdown: str | None = None,
    filename: str = "report.md",
) -> None:
    """Write the html page; *markdown* is the template its comments are exported into, as *filename*."""
    items: list[ReportItem] = []
    errors: list[str] = []
    for source, result in results.items():
        if result.error is not None:
            errors.append(source)
            continue
        items.extend(await result.report_items(persistor))

    sections = triage_sections(items) if mode == FetchMode.triage else todo_sections(items)
    generated = datetime.now().astimezone()
    page = render_page(title, subtitle, sections, generated, errors, markdown, filename)
    path.write_text(page, encoding="utf-8")


def render_page(
    title: str,
    subtitle: str,
    sections: list[tuple[str, list[ReportItem]]],
    generated: datetime,
    errors: list[str] | None = None,
    markdown: str | None = None,
    filename: str = "report.md",
) -> str:
    if markdown is None:
        markdown = sections_markdown(title, sections)
    template = Template((files("startriage") / "data" / "report.html").read_text(encoding="utf-8"))
    body = "".join(_section(name, items) for name, items in sections if items)
    if errors:
        body = f'<p class="error">Fetching failed: {escape(", ".join(errors))}</p>' + body
    return template.substitute(
        title=escape(title),
        subtitle=escape(subtitle),
        generated=escape(generated.strftime("%Y-%m-%d %H:%M")),
        report_id=escape(f"{title} {generated.isoformat()}"),
        filename=escape(filename),
        # json in a script element: only "</" could end it early
        markdown=json.dumps(markdown).replace("</", "<\\/"),
        body=body or "<p>Nothing to do.</p>",
    )


def sections_markdown(title: str, sections: list[tuple[str, list[ReportItem]]]) -> str:
    """Markdown template with a heading per item, which comments are exported below."""
    lines = [f"# {title}", ""]
    for name, items in sections:
        if not items:
            continue
        lines += [f"## {name}", ""]
        for item in items:
            context = f" {item.context_text}" if item.context else ""
            lines += [f"#### [{item.label}]({item.url}){context} \u2014 {item.title}", ""]
    return "\n".join(lines)


def _section(name: str, items: list[ReportItem]) -> str:
    rows = "".join(_row(item) for item in items)
    sources = dict.fromkeys(item.source for item in items)
    context = " / ".join(_CONTEXT_LABELS.get(source, "") for source in sources)
    headers = ["", "Item", context, "Release", "Status", "Prio", "Assignee", "Updated", "Title"]
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    return (
        f'<section><h2>{escape(name)} <span class="count">{len(items)}</span></h2>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _links(links: list[Link], sep: str = " ") -> str:
    return sep.join(
        f'<a href="{escape(link.url)}">{escape(link.text)}</a>' if link.url else escape(link.text)
        for link in links
    )


def _row(item: ReportItem) -> str:
    releases = "".join(
        f'<span class="rel {escape(state)}" title="{escape(state)}">{escape(letter)}</span>'
        for letter, state in item.releases
    )

    chips = ""
    if Flag.GONE not in item.flags:
        chips = "".join(
            f'<span class="chip">{label}</span>' for flag, label in _CHIPS.items() if flag in item.flags
        )
    if chips:
        chips = f'<span class="chips">{chips}</span>'
    further = f'<div class="further">\u21b3 {escape(", ".join(item.further))}</div>' if item.further else ""

    refs = f' <span class="refs">{_links(item.refs)}</span>' if item.refs else ""
    updated = item.updated.strftime("%Y-%m-%d") if item.updated else ""
    assignees = ", ".join(link.text for link in item.assignees)
    row_id = escape(f"{item.source}-{item.key}")
    return (
        f'<tr id="{row_id}" data-url="{escape(item.url)}">'
        '<td><button class="check" title="Done, hide this row">\u2713</button></td>'
        f'<td><a href="{escape(item.url)}">{escape(item.label)}</a></td>'
        f'<td title="{escape(item.context_text)}">{_links(item.context)}</td><td>{releases}</td>'
        f"<td>{escape(item.status)}</td><td>{escape(item.importance)}</td>"
        f'<td title="{escape(assignees)}">{_links(item.assignees, ", ")}</td><td>{updated}</td>'
        f'<td><div class="title"><span>{escape(item.title)}{refs}</span>{chips}'
        '<button class="add-comment reveal" title="Comment for the markdown report">\U0001f4ac</button></div>'
        f'{further}<textarea class="comment" rows="1" placeholder="comment…" hidden></textarea></td>'
        "</tr>"
    )
