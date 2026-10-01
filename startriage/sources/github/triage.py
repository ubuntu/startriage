"""GitHub triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

import aiohttp

from ...config import StarTriageConfig, UpdateFilter
from ...output import (
    Flag,
    Link,
    OutputConfig,
    OutputFormat,
    ReportItem,
    TriageResult,
    hyperlink,
    truncate_string,
)
from ...savebugs import BugPersistor
from ...source import FetchMode, TaskFilterOptions
from .auth import get_github_token
from .finder import _make_headers, fetch_repos, fetch_team_members
from .models import GithubItemEntry, GitHubItemType, Issue, PullRequest, RepoResult

logger = logging.getLogger(__name__)


@dataclass
class GithubTriage(TriageResult):
    """Holds all fetched GitHub results for one triage run."""

    start: date | None
    end: date | None
    results: list[RepoResult] = field(default_factory=list)
    mode: FetchMode = FetchMode.triage
    recent_since: datetime | None = None
    old_since: datetime | None = None
    # logins whose activity counts as ours; None if unknown
    team_members: set[str] | None = None

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
                        f"#### {entry_link}: {truncate_string(entry.item.title, 50)}\n",
                        file=cfg.out,
                    )
                case OutputFormat.TERMINAL:
                    link = hyperlink(entry.url, num_text, cfg.fmt)
                    repo_col = hyperlink(
                        entry.repo_url, truncate_string(entry.repo, repo_w, pad=True), cfg.fmt
                    )
                    assignee_col = (
                        hyperlink(
                            f"https://github.com/{assignee}",
                            truncate_string(assignee, assignee_w),
                            pad_right=assignee_w,
                        )
                        if assignee
                        else " " * assignee_w
                    )
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
                current_keys = {entry.key for entry in items}
                gone = [k for k in former_bugs if k not in current_keys]
                if gone and cfg.bug_persistor:
                    print(f"\nItems gone compared with {cfg.bug_persistor.compare_str}:", file=cfg.out)
                    gone_items = [GithubItemEntry.from_key(k) for k in gone]
                    for item in gone_items:
                        print(f"- {hyperlink(item.url, item.key, cfg.fmt)}", file=cfg.out)
                    print(file=cfg.out)
                elif cfg.bug_persistor:
                    print(f"\nNo items gone compared with {cfg.bug_persistor.compare_str}.", file=cfg.out)

    async def record(self, persistor: BugPersistor) -> None:
        items = self._collect_items()
        item_ids = {entry.key for entry in items}
        persistor.record("github", item_ids)

    async def report_items(self, persistor: BugPersistor | None) -> list[ReportItem]:
        former = persistor.former_bugs("github") if persistor else set()
        entries = self._collect_items()
        current = {entry.key for entry in entries}
        gone = [GithubItemEntry.from_key(k) for k in sorted(former - current)]

        items = []
        for entry in entries + gone:
            updated = entry.item.updated_at or entry.item.created_at
            checks = {
                Flag.GONE: entry.key not in current,
                Flag.NEW: bool(former) and entry.key not in former,
                Flag.RECENT: bool(updated and self.recent_since and updated > self.recent_since),
                Flag.OLD: bool(updated and self.old_since and updated < self.old_since),
                Flag.EXTERNAL: self._is_external(entry),
            }
            items.append(
                ReportItem(
                    source="github",
                    key=entry.key,
                    label=f"{entry.item_type} #{entry.item.number}",
                    url=entry.url,
                    title=entry.item.title,
                    context=[Link(entry.repo, entry.repo_url)],
                    status=entry.item.state,
                    assignees=[Link(a, f"https://github.com/{a}") for a in [entry.item.assignee] if a],
                    updated=updated,
                    flags={flag for flag, check in checks.items() if check},
                )
            )
        return items

    def _is_external(self, entry: GithubItemEntry) -> bool:
        return entry.item.state == "open" and _last_actor_ours(entry.item, self.team_members) is False

    async def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "start": self.start,
            "end": self.end,
            "repos": [asdict(r) for r in self.results],
        }


def _last_actor_ours(item: Issue | PullRequest, team_members: set[str] | None) -> bool | None:
    """Whether the last word, the latest comment or else the opening post, is from the team.

    None if unknown: no team members, or a deleted user.
    """
    last = item.latest_comment_author or item.author
    if team_members is None or last is None:
        return None
    return last in team_members


def _apply_update_filter(
    results: list[RepoResult], update_filter: UpdateFilter, team_members: set[str] | None
) -> None:
    """Drop items whose last actor doesn't match *update_filter*; keep those of unknown actor."""
    if update_filter == UpdateFilter.all:
        return
    keep_ours = update_filter == UpdateFilter.ours

    def keep(item: Issue | PullRequest) -> bool:
        ours = _last_actor_ours(item, team_members)
        return ours is None or ours == keep_ours

    for r in results:
        r.prs = [p for p in r.prs if keep(p)]
        r.issues = [i for i in r.issues if keep(i)]


async def find(
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
) -> GithubTriage:
    """Fetch GitHub data for all repos concurrently."""

    token = get_github_token(config.general.github_token)
    if token:
        logger.debug("fetching github data using github token")
    else:
        logger.debug("fetching github data through anonymous access")

    team_config = config.get_team(filter.team)
    headers = _make_headers(token)

    team_label_list = team_config.github_todo_labels
    if team_label_list is None:
        if team_config.lp_todo_tag:
            team_label_list = [team_config.lp_todo_tag]

    if mode == FetchMode.triage:
        start = filter.start
        end = filter.end
    else:
        start = None
        end = None

    # Build list of (repo_name, labels) for batch fetching
    repo_specs: list[tuple[str, list[str] | None]] = []
    ignore_labels: dict[str, set[str]] = {}
    for repo_cfg in team_config.github_repos:
        labels = None
        if mode == FetchMode.todo:
            if repo_cfg.todo_labels is not None:
                labels = repo_cfg.todo_labels
            else:
                labels = team_label_list
        repo_specs.append((repo_cfg.name, labels))

        ignored = (
            repo_cfg.ignore_labels if repo_cfg.ignore_labels is not None else team_config.github_ignore_labels
        )
        if ignored:
            ignore_labels[repo_cfg.name] = set(ignored)

    async with aiohttp.ClientSession(headers=headers) as session:
        results = await fetch_repos(session, repo_specs, mode, start, end)
        team_members = None
        if team_config.github_team and token:
            team_members = await fetch_team_members(session, team_config.github_team)
            if team_members is None:
                logger.debug(
                    "GitHub team %r is not visible with this token, so external updates are not flagged. "
                    "It needs the read:org scope ('startriage login github --private') "
                    'or, for a fine-grained token, the organization permission "Members: read".',
                    team_config.github_team,
                )

    # Drop results that include at least one of the ignored labels when doing triage.
    if mode == FetchMode.triage:
        for r in results:
            if ignore := ignore_labels.get(r.repo):
                r.prs = [p for p in r.prs if ignore.isdisjoint(p.labels)]
                r.issues = [i for i in r.issues if ignore.isdisjoint(i.labels)]

        update_filter = filter.update_filter or config.general.triage_updates
        _apply_update_filter(results, update_filter, team_members)

    return GithubTriage(
        start=start,
        end=end,
        results=results,
        mode=mode,
        recent_since=filter.recent_since,
        old_since=filter.old_since,
        team_members=team_members,
    )
