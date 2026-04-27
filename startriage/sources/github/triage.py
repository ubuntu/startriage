"""GitHub triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import webbrowser
from dataclasses import dataclass, field
from datetime import date

import aiohttp

from ...config import StarTriageConfig
from ...enums import FetchMode
from ...output import OutputConfig, OutputFormat, TriageResult, hyperlink, truncate_string
from ...savebugs import BugPersistor
from ...source import TaskFilterOptions
from .finder import _make_headers, fetch_repo, get_github_token
from .models import GithubItemEntry, GitHubItemType, RepoResult


@dataclass
class GithubTriage(TriageResult):
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
                rows.append(GithubItemEntry(GitHubItemType.pr, pr.html_url, result.repo, result.repo_url, pr))
            for issue in result.issues:
                rows.append(
                    GithubItemEntry(GitHubItemType.issue, issue.html_url, result.repo, result.repo_url, issue)
                )
        return rows

    async def _print_items(
        self,
        entries: list[GithubItemEntry],
        cfg: OutputConfig,
        former_bugs: set[str],
    ) -> None:
        """Render a unified table of GitHub items; return list of reported item keys."""
        num_w = max(len(str(item.item.number)) for item in entries) + 1  # +1 for '#'
        repo_w = min(35, max(len(item.repo) for item in entries))
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
                        f"### {entry_link}: {truncate_string(entry.item.title, 50)}\n",
                        file=cfg.out,
                    )
                case OutputFormat.TERMINAL:
                    link = hyperlink(entry.url, num_text, cfg.fmt)
                    repo_col = hyperlink(
                        entry.repo_url, truncate_string(entry.repo, repo_w, pad=True), cfg.fmt
                    )
                    assignee_col = truncate_string(assignee, assignee_w, pad=True)
                    row_str = f"{link} | {entry.item_type:<{type_w}} | {repo_col} | {assignee_col}"
                    print(f"{row_str} | {date_str} | {truncate_string(entry.item.title, 50)}", file=cfg.out)
                case _:
                    raise NotImplementedError

        if cfg.open_in_browser:
            for entry in entries:
                webbrowser.open_new_tab(entry.url)
                await asyncio.sleep(0.5)

    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        """
        Print the GitHub section as a unified table.
        """
        items = self._collect_items()
        plural = "item" if len(items) == 1 else "items"

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                print("## GitHub", file=cfg.out)
                if not items:
                    print("no activity", file=cfg.out)
            case OutputFormat.TERMINAL:
                print(f"## GitHub ({len(items)} {plural})", file=cfg.out)
                match self.mode:
                    case FetchMode.triage:
                        print("filter: recently updated", file=cfg.out)
                    case FetchMode.todo | FetchMode.subscribed:
                        print("filter: todo label", file=cfg.out)
                    case _:
                        raise NotImplementedError
            case _:
                raise NotImplementedError

        if cfg.bug_persistor:
            former_bugs = set(cfg.bug_persistor.former_bugs("github"))
        else:
            former_bugs = set()

        if items:
            await self._print_items(items, cfg, former_bugs)

        if cfg.fmt == OutputFormat.TERMINAL:
            print(file=cfg.out)

            if former_bugs:
                gone = [k for k in former_bugs if k not in {entry.key for entry in items}]
                if gone and cfg.bug_persistor:
                    print(f"\nItems gone compared with {cfg.bug_persistor.compare_str}:", file=cfg.out)
                    for key in gone:
                        print(f"  {key}", file=cfg.out)
                    print(file=cfg.out)

    async def record(self, persistor: BugPersistor) -> None:
        items = self._collect_items()
        item_ids = {entry.key for entry in items}
        persistor.record("github", item_ids)

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
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
) -> GithubTriage:
    """Fetch GitHub data for all repos concurrently."""

    token = get_github_token()

    team_config = config.get_team(filter.team)
    headers = _make_headers(token)

    team_label_list = team_config.github_todo_labels
    if team_label_list is None:
        if team_config.lp_todo_tag:
            team_label_list = [team_config.lp_todo_tag]

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = []
        for repo in team_config.github_repos:
            labels = None
            if mode == FetchMode.todo:
                if repo.todo_labels is not None:
                    labels = repo.todo_labels
                else:
                    labels = team_label_list

            tasks.append(
                fetch_repo(
                    session,
                    repo.name,
                    filter.start,
                    filter.end,
                    labels=labels,
                )
            )
        results = await asyncio.gather(*tasks)

    return GithubTriage(start=filter.start, end=filter.end, results=results, mode=mode)
