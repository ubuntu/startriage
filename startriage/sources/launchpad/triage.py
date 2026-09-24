"""Launchpad triage result: holds fetched data and renders output."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import webbrowser
from dataclasses import dataclass, field

import aiohttp
from launchpadlib.launchpad import Launchpad
from lazr.restfulclient.errors import ServerError

from ...config import GeneralConfig, StarTriageConfig, TeamConfig
from ...enums import FetchMode
from ...output import (
    FailedTriageResult,
    OutputConfig,
    OutputFormat,
    TriageResult,
    hyperlink,
    truncate_string,
)
from ...savebugs import BugPersistor
from ...source import TaskFilterOptions
from .finder import connect_launchpad, fetch_bugs, fetch_changelogs
from .models import LaunchpadTasks, RenderContext, Task

logger = logging.getLogger(__name__)


@dataclass
class LaunchpadTriage(TriageResult):
    """Holds all fetched Launchpad results for one triage run."""

    tasks: LaunchpadTasks
    filter: TaskFilterOptions
    team_config: TeamConfig
    config: GeneralConfig
    mode: FetchMode = FetchMode.triage
    # (bugid, pkg) for all bugsfixes waiting in unapproved
    unapproved_bug_fixes: set[tuple[str, str]] = field(default_factory=set)

    @property
    def had_updates(self) -> bool:
        return bool(self.tasks)

    async def print_section(
        self,
        cfg: OutputConfig,
    ) -> None:
        """Show launchpad items."""

        if self.config.lp_extended is not None:
            extended = self.config.lp_extended
        else:
            match self.mode:
                case FetchMode.triage:
                    extended = False
                case FetchMode.todo | FetchMode.subscribed:
                    extended = True
                case _:
                    raise NotImplementedError(f"{self.mode!r}")

        # In todo mode, unassigned tasks get their own section after the main list.
        unassigned: list[Task] = []
        tasks = self.tasks.tasks
        if self.mode == FetchMode.todo:
            unassigned = [t for t in tasks if not t.all_assignees]
            if unassigned:
                tasks = [t for t in tasks if t.all_assignees]

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

        former_bugs = cfg.bug_persistor.former_bugs("launchpad") if cfg.bug_persistor else None

        if bug_count == 0 and not self.tasks.freezer_tasks and not unassigned and not former_bugs:
            return

        ctx = RenderContext(
            nowork_statuses=self.tasks.nowork_statuses,
            open_statuses=self.tasks.open_statuses,
            unapproved_bug_fixes=self.unapproved_bug_fixes,
            recent_since=self.filter.recent_since,
            old_since=self.filter.old_since,
        )

        reported: set[str] = set(
            await _print_bugs(
                tasks,
                ctx,
                cfg,
                extended,
                order_by_date=(self.mode == FetchMode.subscribed),
                former_bugs=former_bugs,
            )
        )

        if self.mode == FetchMode.todo:
            # subsections don't flag new bugs against the compare file
            sub_cfg = dataclasses.replace(cfg, bug_persistor=None)

            if self.tasks.freezer_tasks:
                _print_section_header(
                    "Freezer",
                    self.tasks.freezer_tasks,
                    cfg,
                    extra=f"tag={self.team_config.lp_freezer_tag}",
                )
                reported.update(
                    await _print_bugs(self.tasks.freezer_tasks, ctx, sub_cfg, extended, order_by_date=True)
                )

            if unassigned:
                _print_section_header("Unassigned", unassigned, cfg)
                reported.update(await _print_bugs(unassigned, ctx, sub_cfg, extended))

        if self.mode == FetchMode.triage and self.filter.show_expiration:
            await _print_old_bugs(
                self.tasks.expiring_tagged,
                self.tasks.expiring_subscribed,
                ctx,
                cfg,
                self.config,
                extended,
            )

        # bugs from the compare file that no section listed anymore
        if former_bugs and cfg.bug_persistor:
            closed = sorted(number for number in former_bugs if number not in reported)
            print(f"\nBugs gone compared with {cfg.bug_persistor.compare_str}:", file=cfg.out)
            await _print_bugs(
                _bugs_to_tasks(closed, self.tasks.lp),
                ctx,
                dataclasses.replace(cfg, open_in_browser=False, bug_persistor=None),
                extended,
                is_sorted=True,
            )

    async def record(self, persistor: BugPersistor) -> None:
        ids = {t.number for t in self.tasks.tasks}
        # freezer bugs are still watched -- record them so they don't
        # surface as "gone" in the next comparison
        ids.update(t.number for t in self.tasks.freezer_tasks)
        persistor.record("launchpad", ids)

    def to_json(self) -> str:
        ctx = RenderContext(
            nowork_statuses=self.tasks.nowork_statuses,
            open_statuses=self.tasks.open_statuses,
            unapproved_bug_fixes=self.unapproved_bug_fixes,
            recent_since=self.filter.recent_since,
            old_since=self.filter.old_since,
        )
        return json.dumps([t.to_dict(ctx) for t in self.tasks.tasks], indent=4, default=str)


async def _print_bugs(
    tasks: list[Task],
    ctx: RenderContext,
    cfg: OutputConfig,
    extended: bool,
    order_by_date: bool = False,
    is_sorted: bool = False,
    former_bugs: set[str] | None = None,
) -> list[str]:
    """Render a list of bug tasks as a table; return the bug numbers shown.

    Generic printer for any Launchpad task list (main, freezer, unassigned,
    gone, expiring). Section headers and the gone-listing are the caller's
    concern; *former_bugs* only controls the new-bug flag.
    """

    if is_sorted:
        sorted_tasks = tasks
    else:
        # Task.sort_key is (last_activity_ours, bugid, src)
        sort_key = Task.sort_date if order_by_date else Task.sort_key
        sorted_tasks = sorted(tasks, key=sort_key, reverse=order_by_date)

    if not sorted_tasks:
        print(file=cfg.out)  # trailing newline for spacing after empty section
        return []

    bugid_len = max(len(t.number) for t in sorted_tasks)

    if cfg.fmt == OutputFormat.TERMINAL:
        print(Task.get_table_header(bugid_len, extended=extended), file=cfg.out)

    # Group tasks by bug number, preserving the global sort order of first occurrence.
    # Within each group, sort by actionability so the most-actionable task is primary;
    # the rest are listed as a short "further" line immediately below.
    ordered_numbers: list[str] = list(dict.fromkeys(t.number for t in sorted_tasks))
    groups: dict[str, list[Task]] = {n: [] for n in ordered_numbers}
    for task in sorted_tasks:
        groups[task.number].append(task)

    for number in ordered_numbers:
        group = sorted(groups[number], key=lambda t: t.actionability_rank(ctx))
        primary, further_tasks = group[0], group[1:]

        newbug = bool(former_bugs and number not in former_bugs)

        match cfg.fmt:
            case OutputFormat.MARKDOWN:
                bug_link = hyperlink(primary.url, f"LP #{number}", cfg.fmt)
                print(
                    f"#### {bug_link} {primary.src} \u2014 {truncate_string(primary.short_title, 80)}",
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

    if cfg.open_in_browser:
        for number in ordered_numbers:
            url = groups[number][0].url
            webbrowser.open_new_tab(url)
            await asyncio.sleep(0.2)

    if cfg.fmt == OutputFormat.TERMINAL:
        print(file=cfg.out)  # blank line after bugs for visual separation

    return ordered_numbers


def _bugs_to_tasks(bug_numbers: list[str], lp: Launchpad) -> list[Task]:
    if not lp:
        return []
    tasks = []
    for number in bug_numbers:
        for lp_task in lp.bugs[number].bug_tasks:
            tasks.append(Task(lp_task, subscribed=False, last_activity_ours=False))
    return tasks


def _print_section_header(label: str, tasks: list[Task], cfg: OutputConfig, extra: str = "") -> None:
    count = len({t.number for t in tasks})
    plural = "item" if count == 1 else "items"
    suffix = f", {extra}" if extra else ""
    print(f"### {label} ({count} {plural}{suffix})", file=cfg.out)


async def _print_old_bugs(
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
                    "Expiring level 1",
                    expiring_tagged,
                    config.lp_expire_level1_days,
                    False,
                ),
                (
                    "Expiring level 2",
                    expiring_subscribed,
                    config.lp_expire_level2_days,
                    True,
                ),
            ]:
                if not exp_tasks:
                    continue

                print(file=out_cfg.out)
                _print_section_header(label, exp_tasks, out_cfg, extra=f"~{days} days ago")
                await _print_bugs(exp_tasks, ctx, out_cfg, extended, order_by_date=order_by_date)

        case OutputFormat.MARKDOWN:
            exp_tasks = list(set(expiring_tagged) | set(expiring_subscribed))
            _print_section_header("Old", exp_tasks, out_cfg)
            await _print_bugs(exp_tasks, ctx, out_cfg, extended, order_by_date=True)

        case _:
            raise NotImplementedError


async def find(
    config: StarTriageConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
) -> TriageResult:
    """Fetch Launchpad bugs."""
    effective_update_filter = filter.update_filter or config.general.lp_triage_updates

    team_config = config.get_team(filter.team)

    logger.debug("Logging into Launchpad…")
    lp = connect_launchpad()
    logger.debug("Fetching Launchpad bugs…")
    try:
        lp_tasks = await asyncio.to_thread(
            fetch_bugs,
            lp,
            team_config,
            filter,
            mode,
            effective_update_filter,
            config.general.lp_expire_level1_days,
            config.general.lp_expire_level2_days,
        )
    except ServerError as exc:
        # no traceback for LP internal errors/overloads, but show the OOPS id
        exc_str = str(exc)
        msg = f"{type(exc).__module__}.{type(exc).__qualname__}: {exc_str.splitlines()[0]}"
        if oops := re.search(r"OOPS-[0-9a-f]+", exc_str):
            msg += f" ({oops.group()})"
        return FailedTriageResult(msg)
    logger.info("Launchpad: %d bugs fetched. Checking unapproved queue…", len(lp_tasks.tasks))

    async with aiohttp.ClientSession() as session:
        unapproved_upload_bugs = await fetch_changelogs(session, lp_tasks.changes_pairs)

    unapproved_bugfixes: set[tuple[str, str]] = set()
    for pkg, bug_nums in unapproved_upload_bugs.items():
        for bug_num in bug_nums:
            unapproved_bugfixes.add((bug_num, pkg))

    triage = LaunchpadTriage(
        tasks=lp_tasks,
        filter=filter,
        team_config=team_config,
        config=config.general,
        mode=mode,
        unapproved_bug_fixes=unapproved_bugfixes,
    )
    logger.info("Launchpad: done.")
    return triage
