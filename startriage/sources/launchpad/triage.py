"""Launchpad triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import logging
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from launchpadlib.launchpad import Launchpad

from ...config import GeneralConfig, StarTriageConfig, TeamConfig
from ...enums import FetchMode
from ...output import OutputConfig, OutputFormat, TriageResult, hyperlink, truncate_string
from ...savebugs import BugPersistor
from ...source import TaskFilterOptions
from .finder import connect_launchpad, fetch_bugs, fetch_unapproved_bugs_for_series
from .models import LaunchpadTasks, RenderContext, Task


@dataclass
class LaunchpadTriage(TriageResult):
    """Holds all fetched Launchpad results for one triage run."""

    tasks: LaunchpadTasks
    filter: TaskFilterOptions
    team_config: TeamConfig
    config: GeneralConfig
    mode: FetchMode = FetchMode.triage
    unapproved_cache: dict[tuple[str, str], bool] = field(default_factory=dict)

    @property
    def had_updates(self) -> bool:
        return bool(self.tasks)

    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        """Show launchpad items."""
        extended = self.config.lp_extended

        bug_count = len({t.number for t in self.tasks.tasks})

        match cfg.fmt:
            case OutputFormat.TERMINAL:
                plural = "item" if bug_count == 1 else "items"
                print(f"## Launchpad ({bug_count} {plural})", file=cfg.out)
                match self.mode:
                    case FetchMode.triage:
                        print("filter: recently updated", file=cfg.out)
                    case FetchMode.todo:
                        print(f"filter: tag={self.team_config.lp_todo_tag}", file=cfg.out)
                    case FetchMode.subscribed:
                        print(f"filter: subscribed={self.team_config.lp_team}", file=cfg.out)
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
            recent_since=self.filter.recent_since,
            old_since=self.filter.old_since,
        )
        await _print_bugs(
            self.tasks.lp,
            self.tasks.tasks,
            ctx,
            cfg,
            extended,
            order_by_date=(self.mode == FetchMode.subscribed),
        )

        if self.mode == FetchMode.triage:
            await _print_old_bugs(
                self.tasks.lp,
                self.tasks.expiring_tagged,
                self.tasks.expiring_subscribed,
                ctx,
                cfg,
                self.config,
                extended,
            )

    async def write_markdown(self, path: Path) -> None:
        """Append markdown-formatted output to a file."""
        buf = io.StringIO()
        await self.print_section(OutputConfig(fmt=OutputFormat.MARKDOWN, out=buf))
        with path.open("a", encoding="utf-8") as fd:
            fd.write(buf.getvalue())

    async def record(self, persistor: BugPersistor) -> None:
        ids = {t.number for t in self.tasks.tasks}
        persistor.record("launchpad", ids)

    def to_json(self) -> str:
        ctx = RenderContext(
            nowork_statuses=self.tasks.nowork_statuses,
            open_statuses=self.tasks.open_statuses,
            unapproved_cache=self.unapproved_cache,
            recent_since=self.filter.recent_since,
            old_since=self.filter.old_since,
        )
        return json.dumps([t.to_dict(ctx) for t in self.tasks.tasks], indent=4, default=str)


async def _print_bugs(
    lp: Launchpad,
    tasks: list[Task],
    ctx: RenderContext,
    cfg: OutputConfig,
    extended: bool,
    order_by_date: bool = False,
    is_sorted: bool = False,
    former_bugs: set[str] | None = None,
) -> None:

    if cfg.bug_persistor and former_bugs is None:
        former_bugs = cfg.bug_persistor.former_bugs("launchpad")

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

        newbug = bool(former_bugs and number not in former_bugs)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                bug_link = hyperlink(primary.url, f"LP #{number}", cfg.fmt)
                print(
                    f"### {bug_link} {primary.src} \u2014 {truncate_string(primary.short_title, 80)}",
                    file=cfg.out,
                )
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

        if former_bugs and cfg.bug_persistor:
            closed = [x for x in former_bugs if x not in reported]
            print(f"\nBugs gone compared with {cfg.bug_persistor.compare_str}:", file=cfg.out)
            gone_cfg = dataclasses.replace(cfg, open_in_browser=False)
            await _print_bugs(
                lp,
                _bugs_to_tasks(closed, lp),
                ctx,
                gone_cfg,
                extended,
                is_sorted=True,
                former_bugs=former_bugs,
            )


def _bugs_to_tasks(bug_numbers: list[str], lp: Launchpad) -> list[Task]:
    if not lp:
        return []
    tasks = []
    for number in bug_numbers:
        for lp_task in lp.bugs[number].bug_tasks:
            tasks.append(Task(lp_task, subscribed=False, last_activity_ours=False))
    return tasks


async def _print_old_bugs(
    lp: Launchpad,
    expiring_tagged: list[Task],
    expiring_subscribed: list[Task],
    ctx: RenderContext,
    out_cfg: OutputConfig,
    config: GeneralConfig,
    extended: bool,
) -> None:
    match out_cfg.fmt:
        case OutputFormat.TERMINAL:
            for label, exp_tasks, days, order_by_date in [
                (
                    "Expiring tagged",
                    expiring_tagged,
                    config.lp_expire_tagged,
                    False,
                ),
                (
                    "Expiring subscribed",
                    expiring_subscribed,
                    config.lp_expire,
                    True,
                ),
            ]:
                if not exp_tasks:
                    continue

                exp_count = len({t.number for t in exp_tasks})
                print(file=out_cfg.out)
                plural = "item" if exp_count == 1 else "items"
                print(f"### {label} ({exp_count} {plural}, ~{days} days ago)", file=out_cfg.out)
                await _print_bugs(lp, exp_tasks, ctx, out_cfg, extended, order_by_date=order_by_date)

        case OutputFormat.MARKDOWN:
            exp_tasks = list(set(expiring_tagged) | set(expiring_subscribed))
            exp_count = len({t.number for t in exp_tasks})
            plural = "item" if exp_count == 1 else "items"
            print(f"### Old {plural}", file=out_cfg.out)
            await _print_bugs(lp, exp_tasks, ctx, out_cfg, extended, order_by_date=True)

        case _:
            raise NotImplementedError


async def find(
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
) -> LaunchpadTriage:
    """Fetch Launchpad bugs."""
    effective_update_filter = filter.update_filter or config.general.lp_triage_updates

    team_config = config.get_team(filter.team)

    logging.info("Fetching Launchpad bugs (this may take a while)…")
    lp = connect_launchpad()
    lp_tasks = await asyncio.to_thread(
        fetch_bugs,
        lp,
        team_config,
        filter,
        mode,
        effective_update_filter,
        config.general.lp_expire_tagged,
        config.general.lp_expire,
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
        filter=filter,
        team_config=team_config,
        config=config.general,
        mode=mode,
        unapproved_cache=unapproved_cache,
    )
    logging.info("Launchpad: done.")
    return triage
