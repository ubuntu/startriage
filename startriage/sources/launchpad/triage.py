"""Launchpad triage result: holds fetched data and renders output."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import date
from typing import Literal

import yaml

from startriage.config import GeneralConfig, TeamConfig
from startriage.output import OutputFormat, hyperlink

from .models import STR_STRIKETHROUGH, Task

ANSI_ESCAPE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])",
    re.VERBOSE,
)


@dataclass
class LaunchpadTriage:
    """Holds all fetched Launchpad results for one triage run."""

    tasks: list[Task]
    start: date | None
    end: date | None
    team_config: TeamConfig
    general_config: GeneralConfig
    mode: Literal["triage", "todo", "subscribed"] = "triage"

    @property
    def had_updates(self) -> bool:
        return bool(self.tasks)

    def print_section(
        self,
        fmt: OutputFormat = OutputFormat.TERMINAL,
        open_in_browser: bool = False,
        extended: bool | None = None,
        filename_save: str | None = None,
        filename_compare: str | None = None,
        filename_postponed: str | None = None,
        limit: int | None = None,
        out=None,
    ) -> None:
        """Print the # Bugs section."""
        if out is None:
            out = sys.stdout
        if extended is None:
            extended = self.general_config.lp_extended

        _print = lambda s="": print(s, file=out)  # noqa: E731

        _print("\n# Bugs\n")

        if self.mode == "triage" and self.start and self.end:
            from startriage.dates import reverse_auto_date_range

            pretty_start = self.start.strftime("%Y-%m-%d (%A)")
            pretty_end = self.end.strftime("%Y-%m-%d (%A)")
            if self.start == self.end:
                logging.info("Bugs last updated on %s", pretty_start)
            else:
                logging.info("Bugs last updated between %s and %s inclusive", pretty_start, pretty_end)
            label = reverse_auto_date_range(self.start, self.end)
            if label:
                logging.info('Date range identified as: "%s"', label)

        _print_bugs(
            self.tasks,
            fmt,
            open_in_browser,
            extended,
            filename_save,
            filename_compare,
            filename_postponed,
            limit,
            out,
            order_by_date=(self.mode == "subscribed"),
        )

    def write_markdown(self, path: str, extended: bool | None = None) -> None:
        """Append markdown-formatted output to a file."""
        import io
        import logging as _logging

        if extended is None:
            extended = self.general_config.lp_extended
        buf = io.StringIO()
        root = _logging.getLogger()
        old_level = root.level
        root.setLevel(_logging.WARNING)
        try:
            self.print_section(fmt=OutputFormat.MARKDOWN, extended=extended, out=buf)
        finally:
            root.setLevel(old_level)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(buf.getvalue())

    def to_json(self) -> str:
        return json.dumps([t.to_dict() for t in self.tasks], indent=4, default=str)


def _load_former_bugs(filename_compare: str | None) -> list:
    if not filename_compare:
        return []
    try:
        with open(filename_compare, encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except FileNotFoundError:
        return []


def _load_postponed_bugs(filename_postponed: str | None) -> list[str]:
    from datetime import datetime

    postponed = []
    logging.info("\nPostponed bugs:")
    if filename_postponed:
        try:
            with open(filename_postponed, encoding="utf-8") as f:
                pbugs = yaml.safe_load(f) or []
            for pbug in pbugs:
                until = datetime.strptime(pbug[1], "%Y-%m-%d")
                if until.date() > datetime.now().date():
                    logging.info("%s postponed until %s", pbug[0], pbug[1])
                    postponed.append(pbug[0])
        except FileNotFoundError:
            pass
    if not postponed:
        logging.info("<None>")
    logging.info("")
    return postponed


def _print_bugs(  # noqa: PLR0913
    tasks: list[Task],
    fmt: OutputFormat,
    open_in_browser: bool,
    extended: bool,
    filename_save: str | None,
    filename_compare: str | None,
    filename_postponed: str | None,
    limit: int | None,
    out,
    order_by_date: bool = False,
    is_sorted: bool = False,
    former_bugs: list | None = None,
    postponed_bugs: list[str] | None = None,
) -> None:
    if former_bugs is None:
        former_bugs = _load_former_bugs(filename_compare)
    if postponed_bugs is None:
        postponed_bugs = _load_postponed_bugs(filename_postponed)

    sorted_tasks = (
        tasks
        if is_sorted
        else sorted(tasks, key=Task.sort_date if order_by_date else Task.sort_key, reverse=order_by_date)
    )

    bugid_len = max((len(t.number) for t in sorted_tasks), default=0)

    logging.info("Found %d bugs\n", len(sorted_tasks))
    if not sorted_tasks:
        return

    if limit is not None and len(sorted_tasks) > limit:
        logging.info("Displaying top & bottom %d", limit)
        logging.info("# Recent tasks #")
        _print_bugs(
            sorted_tasks[:limit],
            fmt,
            open_in_browser,
            extended,
            None,
            None,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )
        logging.info("---------------------------------------------------")
        logging.info("# Oldest tasks #")
        _print_bugs(
            sorted_tasks[-limit:],
            fmt,
            open_in_browser,
            extended,
            None,
            None,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )
        return

    if fmt == OutputFormat.TERMINAL:
        logging.info(Task.get_header(extended=extended))

    initial_open = True
    reported: list[str] = []
    further = ""
    for task in sorted_tasks:
        if task.number in reported:
            sep = ", " if further and not further.startswith("Also:") else "Also: "
            further += sep + f"[{task.compose_dup(extended=extended)}]"
            continue
        if further:
            logging.info(further)
            further = ""

        newbug = bool(filename_compare and task.number not in former_bugs)

        if fmt == OutputFormat.MARKDOWN:
            bug_link = hyperlink(task.url, f"LP #{task.number}", fmt)
            print(
                f"### {bug_link} {task.status} {task.src} - {task.short_title}\n",
                file=out,
            )
            print(f"{task.src}: \n", file=out)  # action stub
        else:
            bugtext = task.compose_pretty(bugid_len, shortlinks=True, extended=extended, newbug=newbug)
            if task.number in postponed_bugs:
                bugtext = ANSI_ESCAPE.sub("", bugtext)
                bugtext = STR_STRIKETHROUGH.join(bugtext)
            logging.info(bugtext)

        if open_in_browser:
            if initial_open:
                initial_open = False
                webbrowser.open(task.url)
            else:
                webbrowser.open_new_tab(task.url)
            time.sleep(0.5)

        reported.append(task.number)

    if further:
        logging.info(further)

    if filename_save:
        with open(filename_save, "w", encoding="utf-8") as f:
            yaml.dump(reported, stream=f)
        logging.info("Saved reported bugs in %s", filename_save)

    if filename_compare:
        closed = [x for x in former_bugs if x not in reported]
        logging.info("\nBugs gone compared with %s:", filename_compare)
        _print_bugs(
            _bugs_to_tasks(closed),
            fmt,
            False,
            extended,
            None,
            None,
            None,
            None,
            out,
            is_sorted=True,
            former_bugs=former_bugs,
            postponed_bugs=postponed_bugs,
        )


def _bugs_to_tasks(bug_numbers: list[str]) -> list[Task]:
    from startriage.sources.launchpad.models import Task as TaskModel

    task_class = TaskModel

    lp = task_class.LP
    if not lp:
        return []
    tasks = []
    for number in bug_numbers:
        for lp_task in lp.bugs[number].bug_tasks:
            tasks.append(
                task_class.create_from_launchpadlib_object(
                    lp_task, subscribed=False, last_activity_ours=False
                )
            )
    return tasks
