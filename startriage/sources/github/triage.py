"""GitHub triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

import aiohttp

from startriage.output import OutputFormat, hyperlink
from startriage.savebugs import BugPersistor

from .finder import _make_headers, fetch_repo
from .models import Issue, PullRequest


@dataclass
class RepoResult:
    repo: str
    org: str
    prs: list[PullRequest] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.repo}"

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.org}/{self.repo}"

    @property
    def had_updates(self) -> bool:
        return bool(self.prs or self.issues)


@dataclass
class GithubTriage:
    """Holds all fetched GitHub results for one triage run."""

    org: str
    start: date | None
    end: date | None
    results: list[RepoResult] = field(default_factory=list)
    label: str | None = None

    @property
    def had_updates(self) -> bool:
        return any(r.had_updates for r in self.results)

    async def print_section(
        self,
        fmt: OutputFormat = OutputFormat.TERMINAL,
        open_in_browser: bool = False,
        out=None,
    ) -> None:
        """Print the # Documentation section."""
        if out is None:
            out = sys.stdout
        _print = lambda s="": print(s, file=out)  # noqa: E731

        _print("\n# Documentation\n")

        for result in self.results:
            repo_link = hyperlink(result.repo_url, result.full_name, fmt)
            _print(f"## {repo_link}\n")

            if result.prs:
                _print("Pull Requests:\n")
                for pr in result.prs:
                    pr_link = hyperlink(pr.html_url, f"#{pr.number}", fmt)
                    _print(f"- PR {pr_link}: {pr.title}")
                    if fmt == OutputFormat.MARKDOWN:
                        _print(f"  PR #{pr.number}: ")
                _print()
            else:
                _print("No new or updated pull requests.\n")

            if result.issues:
                _print("Issues:\n")
                for issue in result.issues:
                    issue_link = hyperlink(issue.html_url, f"#{issue.number}", fmt)
                    _print(f"- Issue {issue_link}: {issue.title}")
                    if fmt == OutputFormat.MARKDOWN:
                        _print(f"  Issue #{issue.number}: ")
                _print()
            else:
                _print("No new or updated issues.\n")

    async def write_markdown(self, path: str) -> None:
        """Append markdown-formatted output to a file."""
        buf = io.StringIO()
        await self.print_section(fmt=OutputFormat.MARKDOWN, out=buf)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(buf.getvalue())

    async def print_todo_section(
        self,
        fmt: OutputFormat = OutputFormat.TERMINAL,
        open_in_browser: bool = False,
        bug_persistor: BugPersistor | None = None,
        out=None,
    ) -> None:
        """Print GitHub items as a housekeeping table (one row per PR/issue)."""
        if out is None:
            out = sys.stdout
        _print = lambda s="": print(s, file=out)  # noqa: E731

        _print("\n# GitHub\n")
        if self.label:
            _print(f"label: {self.label}\n")

        former = set(bug_persistor.former_bugs("github") if bug_persistor else [])

        # Collect all items with their type tag and repo name
        rows: list[tuple[str, str, str, Issue | PullRequest]] = []  # (type, url, repo, item)
        for result in self.results:
            for pr in result.prs:
                rows.append(("PR", pr.html_url, result.repo, pr))
            for issue in result.issues:
                rows.append(("Issue", issue.html_url, result.repo, issue))

        if not rows:
            _print("No open items.\n")
            if bug_persistor is not None:
                bug_persistor.record("github", [])
                bug_persistor.flush()
            return

        # Compute column widths from data
        num_w = max(len(str(item.number)) for _, _, _, item in rows) + 1  # +1 for '#'
        repo_w = min(30, max(len(repo) for _, _, repo, _ in rows))
        type_w = 5  # "Issue" is the longest

        if fmt == OutputFormat.TERMINAL:
            header = "%-*s | %-*s | %-*s | %-10s | %s" % (
                num_w + 1,
                "#",
                type_w,
                "Type",
                repo_w,
                "Repo",
                "Updated",
                "Title",
            )
            _print(header)

        reported: list[str] = []
        initial_open = open_in_browser
        for type_str, url, repo, item in rows:
            item_key = f"{self.org}/{repo}#{item.number}"
            is_new = bool(former) and item_key not in former
            new_flag = "N" if is_new else " "
            num_text = f"{new_flag}#{item.number}".rjust(num_w + 1)
            date_dt: datetime | None = item.updated_at or item.created_at
            date_str = date_dt.strftime("%Y-%m-%d") if date_dt else "??-??-??"
            title = item.title

            if fmt == OutputFormat.MARKDOWN:
                link = hyperlink(url, f"#{item.number}", fmt)
                _print(f"### {link} ({type_str}) {repo} \u2014 {title}\n")
                _print(f"{repo}: \n")
            else:
                link = hyperlink(url, num_text, fmt)
                repo_col = repo[:repo_w].ljust(repo_w)
                _print(f"{link} | {type_str:<{type_w}} | {repo_col} | {date_str} | {title}")

            reported.append(item_key)

            if open_in_browser:
                import webbrowser

                if initial_open:
                    webbrowser.open(url)
                    initial_open = False
                else:
                    webbrowser.open_new_tab(url)
                await asyncio.sleep(0.5)

        _print()

        if bug_persistor is not None:
            bug_persistor.record("github", reported)
            bug_persistor.flush()

        if former:
            current_keys = set(reported)
            gone = [k for k in former if k not in current_keys]
            if gone:
                assert bug_persistor is not None
                _print(f"GitHub items gone compared with {bug_persistor.compare_path!r}:")
                for key in gone:
                    _print(f"  {key}")
                _print()

    def to_dict(self) -> dict:
        """Serialise results to a plain dict (JSON-compatible)."""
        return {
            "org": self.org,
            "label": self.label,
            "results": [
                {
                    "repo": r.repo,
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

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=4, default=str)


async def find(
    org: str,
    repos: list[str],
    start: date | None,
    end: date | None,
    token: str | None = None,
    label: str | None = None,
) -> GithubTriage:
    """Fetch GitHub data for all repos concurrently."""
    headers = _make_headers(token)
    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [fetch_repo(session, org, repo, start, end, label) for repo in repos]
        results = await asyncio.gather(*tasks)

    triage = GithubTriage(org=org, start=start, end=end, label=label)
    for repo, (prs, issues) in zip(repos, results, strict=False):
        triage.results.append(RepoResult(repo=repo, org=org, prs=prs, issues=issues))

    return triage
