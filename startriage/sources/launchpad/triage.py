"""Launchpad triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import IO

import aiohttp
from launchpadlib.launchpad import Launchpad

from startriage.config import GeneralConfig, TeamConfig
from startriage.enums import FetchMode
from startriage.output import OutputFormat, hyperlink
from startriage.savebugs import BugPersistor

from .finder import connect_launchpad, fetch_bugs, fetch_unapproved_bugs_for_series
from .models import STR_STRIKETHROUGH, LaunchpadTasks, RenderContext, Task

ANSI_ESCAPE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])",
    re.VERBOSE,
)


@dataclass
class LaunchpadTriage:
    """Holds all fetched Launchpad results for one triage run."""

    tasks: LaunchpadTasks
    start: date | None
    end: date | None
    team_config: TeamConfig
    general_config: GeneralConfig
    mode: FetchMode = FetchMode.triage
    unapproved_cache: dict[tuple[str, str], bool] = field(default_factory=dict)
    age: datetime | None = None
    old: datetime | None = None

    @property
    def had_updates(self) -> bool:
        return bool(self.tasks)

    async def print_section(
        self,
        fmt: OutputFormat = OutputFormat.TERMINAL,
        open_in_browser: bool = False,
        extended: bool | None = None,
        bug_persistor: BugPersistor | None = None,
        limit: int | None = None,
        out: IO[str] | None = None,
    ) -> None:
        """Print the # Bugs section."""
        if out is None:
            out = sys.stdout
        if extended is None:
            extended = self.general_config.lp_extended

        print("# Launchpad Bugs\n", file=out)

        if self.mode == FetchMode.todo:
            print(f"tag: {self.team_config.lp_todo_tag}\n", file=out)
        elif self.mode == FetchMode.subscribed:
            print(f"subscribed: {self.team_config.lp_team}\n", file=out)

        ctx = RenderContext(
            nowork_statuses=self.tasks.nowork_statuses,
            open_statuses=self.tasks.open_statuses,
            unapproved_cache=self.unapproved_cache,
            age=self.age,
            old=self.old,
        )
        await _print_bugs(
            self.tasks,
            ctx,
            fmt,
            open_in_browser,
            extended,
            bug_persistor,
            limit,
            out,
            order_by_date=(self.mode == FetchMode.subscribed),
        )
        if bug_persistor is not None:
            bug_persistor.flush()

    async def write_markdown(self, path: Path, extended: bool | None = None) -> None:
        """Append markdown-formatted output to a file."""
        if extended is None:
            extended = self.general_config.lp_extended
        buf = io.StringIO()
        await self.print_section(fmt=OutputFormat.MARKDOWN, extended=extended, out=buf)
        with path.open("a", encoding="utf-8") as fd:
            fd.write(buf.getvalue())

    def to_json(self) -> str:
        ctx = RenderContext(
            nowork_statuses=self.tasks.nowork_statuses,
            open_statuses=self.tasks.open_statuses,
            unapproved_cache=self.unapproved_cache,
            age=self.age,
            old=self.old,
        )
        return json.dumps([t.to_dict(ctx) for t in self.tasks.tasks], indent=4, default=str)


def _load_former_bugs(bug_persistor: BugPersistor | None) -> list[str]:
    if bug_persistor is None:
        return []
    return bug_persistor.former_bugs("launchpad")


async def _print_bugs(  # noqa: PLR0913
    lp_tasks: LaunchpadTasks,
    ctx: RenderContext,
    fmt: OutputFormat,
    open_in_browser: bool,
    extended: bool,
    bug_persistor: BugPersistor | None,
    limit: int | None,
    out: IO[str],
    order_by_date: bool = False,
    is_sorted: bool = False,
    former_bugs: list[str] | None = None,
    postponed_bugs: list[str] | None = None,
) -> None:
    tasks = lp_tasks.tasks
    if former_bugs is None:
        former_bugs = _load_former_bugs(bug_persistor)
    if postponed_bugs is None and fmt != OutputFormat.MARKDOWN:
        postponed_bugs = bug_persistor.load_postponed(out) if bug_persistor else []

    if is_sorted:
        sorted_tasks = tasks
    else:
        sort_key = Task.sort_date if order_by_date else Task.sort_key
        sorted_tasks = sorted(tasks, key=sort_key, reverse=order_by_date)

    bugid_len = max((len(t.number) for t in sorted_tasks), default=0)

    logging.info("Found %d bugs\n", len(sorted_tasks))
    if not sorted_tasks:
        return

    if limit is not None and len(sorted_tasks) > limit:
        logging.info("Displaying top & bottom %d", limit)
        logging.info("# Recent tasks #")
        await _print_bugs(
            dataclasses.replace(lp_tasks, tasks=sorted_tasks[:limit]),
            ctx,
            fmt,
            open_in_browser,
            extended,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )
        logging.info("---------------------------------------------------")
        logging.info("# Oldest tasks #")
        await _print_bugs(
            dataclasses.replace(lp_tasks, tasks=sorted_tasks[-limit:]),
            ctx,
            fmt,
            open_in_browser,
            extended,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )
        return

    if fmt == OutputFormat.TERMINAL:
        print(Task.get_header(extended=extended), file=out)

    reported: list[str] = []
    further = ""
    for task in sorted_tasks:
        if task.number in reported:
            if fmt != OutputFormat.MARKDOWN:
                arrow = "\N{DOWNWARDS ARROW WITH TIP RIGHTWARDS}"
                if further and not further.startswith(f" {arrow}"):
                    sep = ","
                else:
                    sep = f" {arrow}"
                further += f"{sep} [{task.compose_dup(extended=extended)}]"
            continue
        if further:
            print(further, file=out)
            further = ""

        newbug = bool(bug_persistor and bug_persistor.compare_path and task.number not in former_bugs)

        match fmt:
            case OutputFormat.MARKDOWN:
                bug_link = hyperlink(task.url, f"LP #{task.number}", fmt)
                print(
                    f"### {bug_link} {task.status} - {task.src} - {task.short_title}\n",
                    file=out,
                )
                print("\n", file=out)  # action stub
            case OutputFormat.TERMINAL:
                bugtext = task.get_line(
                    ctx, bugid_len, shortlinks=True, extended=extended, newbug=newbug, fmt=fmt
                )
                if postponed_bugs and task.number in postponed_bugs:
                    # Strip ANSI color codes before applying combining strikethrough,
                    # since inserting U+0336 inside escape sequences would corrupt them.
                    bugtext = ANSI_ESCAPE.sub("", bugtext)
                    bugtext = STR_STRIKETHROUGH.join(bugtext)
                print(bugtext, file=out)
            case _:
                raise ValueError(f"Unknown output format: {fmt!r}")

        reported.append(task.number)

    if open_in_browser:
        initial_open = True
        for task in sorted_tasks:
            if task.number not in reported:
                if initial_open:
                    webbrowser.open(task.url)
                    initial_open = False
                else:
                    webbrowser.open_new_tab(task.url)
                await asyncio.sleep(0.2)

    if further:
        print(further, file=out)

    if bug_persistor is not None and not bug_persistor.no_save:
        bug_persistor.record("launchpad", reported)
        # flush() is called by print_section after this returns

    if bug_persistor is not None and bug_persistor.compare_path:
        closed = [x for x in former_bugs if x not in reported]
        print(f"\nBugs gone compared with {bug_persistor.compare_path!r}:", file=out)
        await _print_bugs(
            dataclasses.replace(lp_tasks, tasks=_bugs_to_tasks(closed, lp_tasks.lp)),
            ctx,
            fmt,
            False,
            extended,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )


def _bugs_to_tasks(bug_numbers: list[str], lp: Launchpad) -> list[Task]:
    if not lp:
        return []
    tasks = []
    for number in bug_numbers:
        for lp_task in lp.bugs[number].bug_tasks:
            tasks.append(
                Task.create_from_launchpadlib_object(lp_task, subscribed=False, last_activity_ours=False)
            )
    return tasks


async def find(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    start_date: date | None,
    end_date: date | None,
    mode: FetchMode = FetchMode.triage,
    update_filter: str | None = None,
    age: datetime | None = None,
    old: datetime | None = None,
) -> LaunchpadTriage:
    """Fetch Launchpad bugs, then bulk-check unapproved queue concurrently."""
    effective_update_filter = update_filter or general_config.lp_update_filter

    logging.info("Fetching Launchpad bugs (this may take a while)…")
    lp = connect_launchpad()
    lp_tasks = await asyncio.to_thread(
        fetch_bugs, lp, team_config, start_date, end_date, mode, effective_update_filter
    )
    logging.info("Launchpad: %d bugs fetched. Checking unapproved queue…", len(lp_tasks.tasks))

    async with aiohttp.ClientSession() as session:
        unapproved_bugs = await fetch_unapproved_bugs_for_series(session, lp_tasks.changes_pairs)

    unapproved_cache: dict[tuple[str, str], bool] = {}
    for pkg, bug_nums in unapproved_bugs.items():
        for bug_num in bug_nums:
            unapproved_cache[(bug_num, pkg)] = True

    triage = LaunchpadTriage(
        tasks=lp_tasks,
        start=start_date,
        end=end_date,
        team_config=team_config,
        general_config=general_config,
        mode=mode,
        unapproved_cache=unapproved_cache,
        age=age,
        old=old,
    )
    logging.info("Launchpad: done.")
    return triage
