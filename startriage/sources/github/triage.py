"""GitHub triage result: holds fetched data and renders output."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

from startriage.output import OutputFormat, hyperlink

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

    @property
    def had_updates(self) -> bool:
        return any(r.had_updates for r in self.results)

    def print_section(
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

    def write_markdown(self, path: str) -> None:
        """Append markdown-formatted output to a file."""
        import io
        import logging as _logging

        buf = io.StringIO()
        root = _logging.getLogger()
        old_level = root.level
        root.setLevel(_logging.WARNING)
        try:
            self.print_section(fmt=OutputFormat.MARKDOWN, out=buf)
        finally:
            root.setLevel(old_level)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
