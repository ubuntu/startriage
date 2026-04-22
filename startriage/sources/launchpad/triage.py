"""Launchpad triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import shlex
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import aiohttp
from launchpadlib.launchpad import Launchpad

from startriage.config import GeneralConfig, TeamConfig
from startriage.enums import FetchMode
from startriage.output import OutputConfig, OutputFormat, TriageOutput, hyperlink
from startriage.savebugs import BugPersistor

from .finder import connect_launchpad, fetch_bugs, fetch_unapproved_bugs_for_series
from .models import LaunchpadTasks, RenderContext, Task


@dataclass
class LaunchpadTriage(TriageOutput):
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
        cfg: OutputConfig,
        *,
        extended: bool | None = None,
        bug_persistor: BugPersistor | None = None,
    ) -> None:
        """Show launchpad items."""
        if extended is None:
            extended = self.general_config.lp_extended

        bug_count = len({t.number for t in self.tasks.tasks})

        match cfg.fmt:
            case OutputFormat.TERMINAL:
                plural = "item" if bug_count == 1 else "items"
                print(f"## Launchpad ({bug_count} {plural})", file=cfg.out)
                match self.mode:
                    case FetchMode.triage:
                        print("filter: recently updated\n", file=cfg.out)
                    case FetchMode.todo:
                        print(f"filter: tag={self.team_config.lp_todo_tag}\n", file=cfg.out)
                    case FetchMode.subscribed:
                        print(f"filter: subscribed={self.team_config.lp_team}\n", file=cfg.out)
                    case _:
                        raise NotImplementedError(f"{self.mode!r}")
            case OutputFormat.MARKDOWN:
                print("## Launchpad", file=cfg.out)
            case _:
                raise NotImplementedError

        if bug_count == 0:
            return

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
            cfg,
            extended,
            bug_persistor,
            order_by_date=(self.mode == FetchMode.subscribed),
        )
        if bug_persistor is not None:
            bug_persistor.save()

    async def write_markdown(self, path: Path) -> None:
        """Append markdown-formatted output to a file."""
        buf = io.StringIO()
        await self.print_section(OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf))
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


async def _print_bugs(
    lp_tasks: LaunchpadTasks,
    ctx: RenderContext,
    cfg: OutputConfig,
    extended: bool,
    bug_persistor: BugPersistor | None,
    order_by_date: bool = False,
    is_sorted: bool = False,
    former_bugs: list[str] | None = None,
) -> None:
    tasks = lp_tasks.tasks
    if former_bugs is None:
        former_bugs = _load_former_bugs(bug_persistor)

    if is_sorted:
        sorted_tasks = tasks
    else:
        # Task.sort_key is (last_activity_ours, bugid, src)
        sort_key = Task.sort_date if order_by_date else Task.sort_key
        sorted_tasks = sorted(tasks, key=sort_key, reverse=order_by_date)

    bugid_len = max((len(t.number) for t in sorted_tasks), default=0)

    if not sorted_tasks:
        print(file=cfg.out)  # trailing newline for spacing after empty section
        return

    if cfg.fmt == OutputFormat.TERMINAL:
        print(Task.get_table_header(extended=extended), file=cfg.out)

    # Group tasks by bug number, preserving the global sort order of first occurrence.
    # Within each group, sort by actionability so the most-actionable task is primary;
    # the rest are listed as a short "further" line immediately below.
    ordered_numbers: list[str] = list(dict.fromkeys(t.number for t in sorted_tasks))
    groups: dict[str, list[Task]] = {n: [] for n in ordered_numbers}
    for task in sorted_tasks:
        groups[task.number].append(task)

    reported: list[str] = []
    for number in ordered_numbers:
        group = sorted(groups[number], key=lambda t: t.actionability_rank(ctx))
        primary, further_tasks = group[0], group[1:]

        newbug = bool(bug_persistor and bug_persistor.compare_path and number not in former_bugs)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                bug_link = hyperlink(primary.url, f"LP #{number}", cfg.fmt)
                print(f"### {bug_link} {primary.src} \u2014 {primary.short_title}", file=cfg.out)
                print(file=cfg.out)  # blank line as space for triager's report

            case OutputFormat.TERMINAL:
                bugtext = primary.get_table_row(
                    ctx,
                    bugid_len,
                    shortlinks=True,
                    extended=extended,
                    newbug=newbug,
                )
                print(bugtext, file=cfg.out)
                if further_tasks:
                    further_tasks_strs = [d.compose_dup(extended=extended) for d in further_tasks]
                    arrow = "\N{DOWNWARDS ARROW WITH TIP RIGHTWARDS}"
                    print(f" {arrow} {', '.join(further_tasks_strs)}", file=cfg.out)

            case _:
                raise NotImplementedError

        reported.append(number)

    if cfg.open_in_browser:
        for number in ordered_numbers:
            url = groups[number][0].url
            webbrowser.open_new_tab(url)
            await asyncio.sleep(0.2)

    if cfg.fmt == OutputFormat.TERMINAL:
        print(file=cfg.out)  # blank line after bugs for visual separation

    if bug_persistor is not None and not bug_persistor.no_save:
        bug_persistor.record("launchpad", reported)
        # flush() is called by print_section after this returns

    if bug_persistor is not None and bug_persistor.compare_path:
        closed = [x for x in former_bugs if x not in reported]
        print(f"\nBugs gone compared with {shlex.quote(str(bug_persistor.compare_path))}:", file=cfg.out)
        gone_cfg = dataclasses.replace(cfg, open_in_browser=False)
        await _print_bugs(
            dataclasses.replace(lp_tasks, tasks=_bugs_to_tasks(closed, lp_tasks.lp)),
            ctx,
            gone_cfg,
            extended,
            None,
            is_sorted=True,
            former_bugs=former_bugs,
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
    show_expiration: bool = True,
) -> LaunchpadTriage:
    """Fetch Launchpad bugs, then bulk-check unapproved queue concurrently."""
    effective_update_filter = update_filter or general_config.lp_triage_updates

    logging.info("Fetching Launchpad bugs (this may take a while)…")
    lp = connect_launchpad()
    lp_tasks = await asyncio.to_thread(
        fetch_bugs,
        lp,
        team_config,
        start_date,
        end_date,
        mode,
        effective_update_filter,
        show_expiration and mode == FetchMode.triage,
        general_config.lp_expire_tagged,
        general_config.lp_expire,
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
