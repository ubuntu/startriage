"""Render triage and todo results as one self-contained html page with checkable rows."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from string import Template

from .analysis import ActionKind, ActionReadiness, AIRun, AnalysisState, ItemAnalysis, RecommendedAction
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

# launchpad importances, most important first
_IMPORTANCE = ["Critical", "High", "Medium", "Low", "Wishlist", "Undecided", "Unknown"]


# empty values sort last, like the page's column sorting
def _by_date(item: ReportItem) -> tuple:
    return (item.updated is None, item.updated or datetime.min)


def _by_assignee(item: ReportItem) -> tuple:
    assignees = ", ".join(link.text for link in item.assignees).casefold()
    return (not assignees, assignees, *_by_date(item))


def todo_sections(items: list[ReportItem]) -> list[tuple[str, list[ReportItem]]]:
    """Put each item into the first matching housekeeping section, by assignee, then stalest first."""
    sections: dict[str, list[ReportItem]] = {name: [] for name in _TODO_ORDER}
    for item in items:
        name = next(name for name, match in _TODO_RULES if match(item))
        sections[name].append(item)
    return [(name, sorted(section, key=_by_assignee)) for name, section in sections.items()]


def triage_sections(items: list[ReportItem]) -> list[tuple[str, list[ReportItem]]]:
    """One section per source, oldest first."""
    sections: dict[str, list[ReportItem]] = {name: [] for name in _TRIAGE_ORDER}
    for item in items:
        sections.setdefault(item.source, []).append(item)
    return [
        (_TRIAGE_TITLES.get(name, name.capitalize()), sorted(section, key=_by_date))
        for name, section in sections.items()
    ]


async def write_report(
    path: Path,
    results: Mapping[str, TriageResult],
    mode: FetchMode,
    title: str,
    subtitle: str,
    persistor: BugPersistor | None = None,
    markdown: str | None = None,
    filename: str = "report.md",
    ai_run: AIRun | None = None,
) -> None:
    """Write the html page; *markdown* is the template its comments are exported into, as *filename*."""
    items: list[ReportItem] = []
    errors: list[str] = []
    for source, result in results.items():
        if result.error is not None:
            errors.append(source)
            continue
        items.extend(await result.report_items(persistor))

    if mode == FetchMode.triage:
        sections, sorted_by = triage_sections(items), "Updated"
    else:
        sections, sorted_by = todo_sections(items), "Assignee"
    generated = datetime.now().astimezone()
    page = render_page(
        title, subtitle, sections, generated, errors, markdown, filename, sorted_by, ai_run=ai_run
    )
    path.write_text(page, encoding="utf-8")


def render_page(
    title: str,
    subtitle: str,
    sections: list[tuple[str, list[ReportItem]]],
    generated: datetime,
    errors: list[str] | None = None,
    markdown: str | None = None,
    filename: str = "report.md",
    sorted_by: str | None = None,
    ai_run: AIRun | None = None,
) -> str:
    """*sorted_by* is the header of the column the rows are sorted by, leading key."""
    if markdown is None:
        markdown = sections_markdown(title, sections)
    ai_by_item = ai_run.by_item() if ai_run is not None else {}
    has_ai = bool(ai_by_item)
    template = Template((files("startriage") / "data" / "report.html").read_text(encoding="utf-8"))
    body = "".join(_section(name, items, sorted_by, ai_by_item) for name, items in sections if items)
    if errors:
        body = f'<p class="error">Fetching failed: {escape(", ".join(errors))}</p>' + body

    ai_controls = (
        '<button id="toggle-all-ai" class="btn" title="Expand or collapse all AI analysis panels">'
        "Expand AI</button>"
        if has_ai
        else ""
    )
    ai_footer = ""
    if ai_run is not None and has_ai:
        tokens_in, tokens_out = ai_run.total_tokens()
        parts = [f"model {ai_run.model}"] if ai_run.model else []
        if tokens_in or tokens_out:
            parts.append(f"{tokens_in:,} in / {tokens_out:,} out tokens")
        if parts:
            ai_footer = f" · AI: {escape(', '.join(parts))}"

    ai_script = (
        f'<script type="application/json" id="ai-data">'
        f"{json.dumps(ai_run.to_dict()).replace('</', '<\\/')}</script>\n"
        if ai_run is not None and has_ai
        else ""
    )

    return template.substitute(
        title=escape(title),
        subtitle=escape(subtitle),
        generated=escape(generated.strftime("%Y-%m-%d %H:%M")),
        report_id=escape(f"{title} {generated.isoformat()}"),
        filename=escape(filename),
        # json in a script element: only "</" could end it early
        markdown=json.dumps(markdown).replace("</", "<\\/"),
        ai_controls=ai_controls,
        ai_footer=ai_footer,
        ai_data=ai_script,
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


def _section(
    name: str,
    items: list[ReportItem],
    sorted_by: str | None,
    ai_by_item: Mapping[tuple[str, str], ItemAnalysis] | None = None,
) -> str:
    rows = "".join(
        _row(item, ai_by_item.get((item.source, item.key)) if ai_by_item else None) for item in items
    )
    sources = dict.fromkeys(item.source for item in items)
    context = " / ".join(_CONTEXT_LABELS.get(source, "") for source in sources)
    headers = ["", "Item", context, "Release", "Status", "Prio", "Assignee", "Updated", "Title"]
    head = ""
    for header in headers:
        sort = ' aria-sort="ascending"' if header and header == sorted_by else ""
        button = f'<button class="sort" title="Sort, ties keep the previous order">{escape(header)}</button>'
        head += f"<th{sort}>{button if header else ''}</th>"
    return (
        f'<section><h2>{escape(name)} <span class="count">{len(items)}</span></h2>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></section>"
    )


def _links(links: list[Link], sep: str = " ") -> str:
    return sep.join(
        f'<a href="{escape(link.url)}">{escape(link.text)}</a>' if link.url else escape(link.text)
        for link in links
    )


_INLINE_TOKEN = re.compile(
    r"`([^`]+)`"
    r"|\[([^\]]+)\]\((https?://[^)\s]+)\)"
    r"|\*\*([^*]+)\*\*"
    r"|(https?://[^\s<>()]+)"
)
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_LIST_ITEM = re.compile(r"^[-*]\s+(.*)$")


def _inline_markdown(text: str) -> str:
    """Render inline code, bold, and http(s) links to escaped HTML."""
    out: list[str] = []
    pos = 0
    for match in _INLINE_TOKEN.finditer(text):
        out.append(escape(text[pos : match.start()]))
        code, link_text, link_url, bold, bare_url = match.groups()
        if code is not None:
            out.append(f"<code>{escape(code)}</code>")
        elif link_text is not None and link_url is not None:
            out.append(f'<a href="{escape(link_url)}">{escape(link_text)}</a>')
        elif bold is not None:
            out.append(f"<strong>{escape(bold)}</strong>")
        elif bare_url is not None:
            trimmed = bare_url.rstrip(".,;:!?")
            trail = bare_url[len(trimmed) :]
            out.append(f'<a href="{escape(trimmed)}">{escape(trimmed)}</a>{escape(trail)}')
        pos = match.end()
    out.append(escape(text[pos:]))
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """Render a safe markdown subset (paragraphs, lists, headings, code, links, bold) to HTML."""
    lines = text.strip().splitlines()
    if not lines:
        return ""
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_fence = False
    fence_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(_inline_markdown(line) for line in paragraph)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append(
                "<ul>" + "".join(f"<li>{_inline_markdown(item)}</li>" for item in list_items) + "</ul>"
            )
            list_items.clear()

    for line in lines:
        stripped = line.strip()
        if in_fence:
            if stripped.startswith("```"):
                blocks.append(f"<pre><code>{escape(chr(10).join(fence_lines))}</code></pre>")
                fence_lines.clear()
                in_fence = False
            else:
                fence_lines.append(line)
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            in_fence = True
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if heading := _HEADING.match(stripped):
            flush_paragraph()
            flush_list()
            blocks.append(f'<div class="ai-heading">{_inline_markdown(heading.group(2))}</div>')
            continue
        if item := _LIST_ITEM.match(stripped):
            flush_paragraph()
            list_items.append(item.group(1))
            continue
        flush_list()
        paragraph.append(line)

    if in_fence:
        blocks.append(f"<pre><code>{escape(chr(10).join(fence_lines))}</code></pre>")
    flush_paragraph()
    flush_list()
    return "".join(blocks)


def _ai_pill(analysis: ItemAnalysis) -> str:
    """Compact inline toggle button for a row's AI analysis."""
    if analysis.state is AnalysisState.failed:
        label, tone = "AI failed", "failed"
    elif analysis.state is AnalysisState.denied:
        label, tone = "private", "muted"
    elif analysis.state is AnalysisState.skipped:
        label, tone = "skipped", "muted"
    elif analysis.duplicate_of:
        label, tone = f"Dup #{analysis.duplicate_of}", "action"
    elif all(a.kind is ActionKind.none for a in analysis.actions):
        label, tone = "no action", "muted"
    elif analysis.status and analysis.status != "no-change":
        label = analysis.status
        if analysis.has_fix:
            label += " · fix"
        tone = "fix" if analysis.has_fix else "action"
    else:
        label = "review"
        if analysis.has_fix:
            label += " · fix"
        tone = "fix" if analysis.has_fix else "muted"

    return (
        f'<button type="button" class="ai-toggle ai-{tone}" aria-expanded="false" '
        f'title="Show AI analysis">\u2726 {escape(label)}</button>'
    )


def _action_pill(action: RecommendedAction) -> str:
    tone = {
        ActionReadiness.ready: "ready",
        ActionReadiness.needs_review: "review",
        ActionReadiness.info_only: "info",
    }[action.readiness]
    return (
        f'<span class="ai-action ai-action-{tone}" '
        f'data-kind="{escape(action.kind.value)}">{escape(action.label)}</span>'
    )


def _ai_panel(analysis: ItemAnalysis) -> str:
    """Expandable inline AI drawer spanning columns 2..-1 of the item's row."""
    if analysis.state is AnalysisState.failed:
        parts = [f'<p class="error"><strong>Error:</strong> {escape(analysis.error)}</p>']
        if analysis.phases:
            parts.append(_ai_phases(analysis))
        return f'<td class="ai-cell" hidden><div class="ai-panel">{"".join(parts)}</div></td>'

    if analysis.state in (AnalysisState.denied, AnalysisState.skipped):
        note = escape(analysis.note or analysis.route)
        parts = [f'<p class="ai-muted">{note}</p>']
        if analysis.phases:
            parts.append(_ai_phases(analysis))
        return f'<td class="ai-cell" hidden><div class="ai-panel">{"".join(parts)}</div></td>'

    meta: list[str] = []
    if analysis.status:
        status_html = f"<strong>{escape(analysis.status)}</strong>"
        if analysis.duplicate_of:
            dup_url = f"https://bugs.launchpad.net/bugs/{analysis.duplicate_of}"
            status_html += f' (<a href="{escape(dup_url)}">LP #{escape(analysis.duplicate_of)}</a>)'
        meta.append(f"<span>Status: {status_html}</span>")
    if analysis.importance and analysis.importance != "Undecided":
        meta.append(f"<span>Importance: <strong>{escape(analysis.importance)}</strong></span>")
    if analysis.series:
        series_str = ", ".join(f"{name}: {state}" for name, state in analysis.series.items())
        meta.append(f"<span>Series: {escape(series_str)}</span>")
    if analysis.tags:
        tags_html = " ".join(f'<span class="chip">{escape(t)}</span>' for t in analysis.tags)
        meta.append(f'<span class="ai-tags">Tags: {tags_html}</span>')
    if analysis.route:
        meta.append(f'<span class="ai-muted">route: {escape(analysis.route)}</span>')

    parts = [f'<div class="ai-meta">{"".join(meta)}</div>']

    if analysis.actions:
        pills = "".join(_action_pill(action) for action in analysis.actions)
        copy_btn = (
            '<button type="button" class="ai-copy-task" title="Copy structured task JSON for automation">'
            "Copy task JSON</button>"
        )
        parts.append(f'<div class="ai-actions"><div class="ai-action-list">{pills}</div>{copy_btn}</div>')

    if analysis.analysis.strip():
        parts.append(f'<div class="ai-body">{markdown_to_html(analysis.analysis)}</div>')

    if analysis.suggested_comment.strip():
        parts.append(
            '<div class="ai-block">'
            '<div class="ai-block-head"><span>Suggested comment</span>'
            '<button type="button" class="ai-copy-comment">Copy</button></div>'
            f'<blockquote class="ai-comment">{escape(analysis.suggested_comment.strip())}</blockquote>'
            "</div>"
        )

    if analysis.has_fix or analysis.fix_applies:
        fix_parts: list[str] = []
        if analysis.proposed_fix.kind == "reference":
            val = analysis.proposed_fix.value.strip()
            link = (
                f'<a href="{escape(val)}">{escape(val)}</a>'
                if val.startswith(("http://", "https://"))
                else escape(val)
            )
            unmerged = ' <span class="chip">unmerged</span>' if analysis.proposed_fix.unmerged else ""
            fix_parts.append(f"<p>{link}{unmerged}</p>")
        elif analysis.proposed_fix.kind == "diff":
            fix_parts.append(
                f'<pre class="ai-diff"><code>{escape(analysis.proposed_fix.value.strip())}</code></pre>'
            )
        if analysis.fix_applies:
            checks = "".join(
                f"<li><strong>{escape(ref)}</strong>: {escape(state)}</li>"
                for ref, state in analysis.fix_applies.items()
            )
            fix_parts.append(f'<ul class="ai-checks">{checks}</ul>')
        parts.append(
            '<div class="ai-block"><div class="ai-block-head"><span>Proposed fix</span></div>'
            + "".join(fix_parts)
            + "</div>"
        )

    if analysis.sru_candidates:
        sru_items = "".join(
            f"<li><strong>{escape(c.series)}</strong>: {escape(c.reason)}</li>"
            for c in analysis.sru_candidates
        )
        parts.append(
            '<div class="ai-block"><div class="ai-block-head"><span>SRU candidates</span></div>'
            f"<ul>{sru_items}</ul></div>"
        )

    if analysis.upstream_suggestion.strip():
        parts.append(
            '<div class="ai-block"><div class="ai-block-head"><span>Upstream</span></div>'
            f"<p>{_inline_markdown(analysis.upstream_suggestion.strip())}</p></div>"
        )

    if analysis.references:
        ref_items = "".join(f"<li>{_inline_markdown(ref)}</li>" for ref in analysis.references)
        parts.append(
            '<div class="ai-block"><div class="ai-block-head"><span>References</span></div>'
            f"<ul>{ref_items}</ul></div>"
        )

    if analysis.phases:
        parts.append(_ai_phases(analysis))

    return f'<td class="ai-cell" hidden><div class="ai-panel">{"".join(parts)}</div></td>'


def _ai_phases(analysis: ItemAnalysis) -> str:
    rows: list[str] = []
    for phase in analysis.phases:
        line = f"<strong>{escape(phase.phase)}</strong> ({escape(phase.state)})"
        if phase.summary:
            line += f": {escape(' '.join(phase.summary.split()))}"
        details: list[str] = []
        if phase.tool_calls:
            details.append(f"tools: {', '.join(dict.fromkeys(phase.tool_calls))}")
        if phase.sessions:
            details.append(f"{phase.input_tokens:,} in / {phase.output_tokens:,} out")
        if details:
            line += f' <span class="ai-muted">({escape("; ".join(details))})</span>'
        rows.append(f"<li>{line}</li>")
    return f'<details class="ai-phases"><summary>Thought process</summary><ul>{"".join(rows)}</ul></details>'


def _row(item: ReportItem, analysis: ItemAnalysis | None = None) -> str:
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
    prio = f' data-sort="{_IMPORTANCE.index(item.importance)}"' if item.importance in _IMPORTANCE else ""
    row_id = escape(f"{item.source}-{item.key}")
    ai_pill = _ai_pill(analysis) if analysis is not None else ""
    ai_drawer = _ai_panel(analysis) if analysis is not None else ""
    return (
        f'<tr id="{row_id}" data-url="{escape(item.url)}">'
        '<td><button class="check" title="Done, hide this row">\u2713</button></td>'
        f'<td><a href="{escape(item.url)}">{escape(item.label)}</a></td>'
        f'<td title="{escape(item.context_text)}">{_links(item.context)}</td><td>{releases}</td>'
        f"<td>{escape(item.status)}</td><td{prio}>{escape(item.importance)}</td>"
        f'<td title="{escape(assignees)}">{_links(item.assignees, ", ")}</td><td>{updated}</td>'
        f'<td><div class="title"><span>{escape(item.title)}{refs}</span>{chips}{ai_pill}'
        '<button class="add-comment reveal" title="Comment for the markdown report">\U0001f4ac</button></div>'
        f'{further}<textarea class="comment" rows="1" placeholder="comment…" hidden></textarea></td>'
        f"{ai_drawer}"
        "</tr>"
    )
