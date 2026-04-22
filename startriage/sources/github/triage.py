"""GitHub triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import io
import webbrowser
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import aiohttp

from startriage.config import GithubRepoConfig
from startriage.enums import FetchMode
from startriage.output import OutputConfig, OutputFormat, TriageOutput, hyperlink, truncate_string
from startriage.savebugs import BugPersistor

from .finder import _make_headers, fetch_repo
from .models import GithubItemEntry, GitHubItemType, RepoResult


@dataclass
class GithubTriage(TriageOutput):
    """Holds all fetched GitHub results for one triage run."""

    start: date | None
    end: date | None
    results: list[RepoResult] = field(default_factory=list)
    mode: FetchMode = FetchMode.triage

    @property
    def had_updates(self) -> bool:
        return any(r.had_updates for r in self.results)

    def _collect_items(self) -> list[GithubItemEntry]:
        """Return _GithubItemRow instances for all PRs and issues."""
        rows: list[GithubItemEntry] = []
        for result in self.results:
            for pr in result.prs:
                rows.append(GithubItemEntry(GitHubItemType.pr, pr.html_url, result.repo, pr))
            for issue in result.issues:
                rows.append(GithubItemEntry(GitHubItemType.issue, issue.html_url, result.repo, issue))
        return rows

    async def _print_items(
        self,
        entries: list[GithubItemEntry],
        cfg: OutputConfig,
        former_bugs: set[str],
    ) -> None:
        """Render a unified table of GitHub items; return list of reported item keys."""
        num_w = max(len(str(item.item.number)) for item in entries) + 1  # +1 for '#'
        repo_w = min(30, max(len(item.repo) for item in entries))
        type_w = 5  # "Issue" is the longest
        assignee_w = 12

        # print a header
        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                ...

            case OutputFormat.TERMINAL:
                header = "%-*s | %-*s | %-*s | %-*s | %-10s | %s" % (
                    num_w + 1,
                    "#",
                    type_w,
                    "Type",
                    repo_w,
                    "Repo",
                    assignee_w,
                    "Assignee",
                    "Updated",
                    "Title",
                )
                print(header, file=cfg.out)
            case _:
                raise NotImplementedError

        for entry in entries:
            item_key = f"{entry.repo}#{entry.item.number}"
            is_new = bool(former_bugs) and item_key not in former_bugs
            new_flag = "N" if is_new else " "
            num_text = f"{new_flag}#{entry.item.number}".rjust(num_w + 1)
            date_dt = entry.item.updated_at or entry.item.created_at
            date_str = date_dt.strftime("%Y-%m-%d") if date_dt else "??-??-??"
            assignee = entry.item.assignee or ""

            match cfg.fmt:
                case OutputFormat.MARKDOWN:
                    entry_link = hyperlink(entry.url, f"{entry.item_type} {item_key}", cfg.fmt)
                    print(
                        f"### {entry_link}: {truncate_string(entry.item.title, 40)}\n",
                        file=cfg.out,
                    )
                case OutputFormat.TERMINAL:
                    link = hyperlink(entry.url, num_text, cfg.fmt)
                    repo_col = truncate_string(entry.repo, repo_w, pad=True)
                    assignee_col = truncate_string(assignee, assignee_w, pad=True)
                    row_str = f"{link} | {entry.item_type:<{type_w}} | {repo_col} | {assignee_col}"
                    print(f"{row_str} | {date_str} | {truncate_string(entry.item.title, 40)}", file=cfg.out)
                case _:
                    raise NotImplementedError

        if cfg.open_in_browser:
            for entry in entries:
                webbrowser.open_new_tab(entry.url)
                await asyncio.sleep(0.5)

    async def print_section(
        self,
        cfg: OutputConfig,
        *,
        bug_persistor: BugPersistor | None = None,
    ) -> None:
        """
        Print the GitHub section as a unified table.
        """
        items = self._collect_items()
        plural = "item" if len(items) == 1 else "items"

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                print("## GitHub", file=cfg.out)
            case OutputFormat.TERMINAL:
                print(f"## GitHub ({len(items)} {plural})", file=cfg.out)
            case _:
                raise NotImplementedError

        if not items:
            match cfg.fmt:
                case OutputFormat.MARKDOWN:
                    print("no activity", file=cfg.out)
                case OutputFormat.TERMINAL:
                    ...
                case _:
                    raise NotImplementedError

            if bug_persistor:
                bug_persistor.record("github", [])
                bug_persistor.save()
            return

        if bug_persistor:
            former_bugs = set(bug_persistor.former_bugs("github"))
        else:
            former_bugs = set()

        await self._print_items(items, cfg, former_bugs)
        print(file=cfg.out)

        item_ids = {f"{entry.repo}#{entry.item.number}" for entry in items}

        if bug_persistor:
            bug_persistor.record("github", list(item_ids))
            bug_persistor.save()

        if former_bugs:
            gone = [k for k in former_bugs if k not in item_ids]
            if gone:
                assert bug_persistor is not None
                print(f"GitHub items gone compared with {bug_persistor.compare_path!r}:", file=cfg.out)
                for key in gone:
                    print(f"  {key}", file=cfg.out)
                print(file=cfg.out)

    async def write_markdown(self, path: Path) -> None:
        """Append markdown-formatted output to a file."""
        buf = io.StringIO()
        await self.print_section(OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(buf.getvalue())

    def to_dict(self) -> dict:
        """Serialise results to a plain dict (JSON-compatible)."""
        return {
            "results": [
                {
                    "repo": r.repo,
                    "labels": r.labels,
                    "prs": [
                        {
                            "number": pr.number,
                            "title": pr.title,
                            "url": pr.html_url,
                            "updated_at": str(pr.updated_at),
                            "labels": pr.labels,
                        }
                        for pr in r.prs
                    ],
                    "issues": [
                        {
                            "number": i.number,
                            "title": i.title,
                            "url": i.html_url,
                            "updated_at": str(i.updated_at),
                            "labels": i.labels,
                        }
                        for i in r.issues
                    ],
                }
                for r in self.results
            ],
        }


async def find(
    repos: list[GithubRepoConfig],
    start: date | None,
    end: date | None,
    token: str | None = None,
    default_label: str | None = None,
    mode: FetchMode = FetchMode.triage,
) -> GithubTriage:
    """Fetch GitHub data for all repos concurrently."""
    headers = _make_headers(token)

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [
            fetch_repo(
                session,
                repo.name,
                start,
                end,
                repo.todo_labels or ([default_label] if default_label else None),
            )
            for repo in repos
        ]
        results = await asyncio.gather(*tasks)

    return GithubTriage(start=start, end=end, results=results, mode=mode)
