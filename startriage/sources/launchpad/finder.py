"""Launchpad bug fetcher for startriage."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp
import debian.deb822
import platformdirs
from launchpadlib.credentials import UnencryptedFileCredentialStore
from launchpadlib.launchpad import Launchpad
from lazr.restfulclient.errors import ClientError

from startriage.source import TaskFilterOptions

from ...config import TeamConfig
from ...enums import FetchMode
from .models import LaunchpadTasks, Task

# apparently not exported by launchpadlib...
POSSIBLE_BUG_STATUSES = [
    "New",
    "Incomplete",
    "Opinion",
    "Invalid",
    "Won't Fix",
    "Expired",
    "Confirmed",
    "Triaged",
    "In Progress",
    "Fix Committed",
    "Fix Released",
]
OPEN_BUG_STATUSES = ["New", "Confirmed", "Triaged", "In Progress", "Fix Committed"]
NOWORK_BUG_STATUSES = ["Opinion", "Invalid", "Won't Fix", "Expired", "Fix Released"]
TRACKED_BUG_STATUSES = [*OPEN_BUG_STATUSES, "Incomplete"]

PACKAGING_TASK_TAGS = [
    "needs-merge",
    "needs-sync",
    "needs-oci-update",
    "needs-snap-update",
    "needs-mre-backport",
    "needs-ppa-backport",
]


def connect_launchpad() -> Launchpad:
    cred_dir = platformdirs.user_data_path("startriage")
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_location = cred_dir / "lp_creds"
    credential_store = UnencryptedFileCredentialStore(str(cred_location))
    return Launchpad.login_with(
        "startriage", "production", version="devel", credential_store=credential_store
    )


def _fast_target_name(obj) -> str:
    return obj.target_link.split("/")[-1]


def _search_tasks_all_series(distro, *args, **kwargs):
    """Search structural/subscriber tasks across all active series (LP #314432 workaround)."""
    result = {(task.bug_link, _fast_target_name(task)): task for task in distro.searchTasks(*args, **kwargs)}
    for series in distro.series_collection:
        if not series.active:
            continue
        result.update(
            {(task.bug_link, _fast_target_name(task)): task for task in series.searchTasks(*args, **kwargs)}
        )
    return result.values()


def _last_activity_ours(
    task_obj, activity_subscriber_links: set[str], last_messages_considered: int = 3
) -> bool:
    if not activity_subscriber_links:
        return False
    activity_list = []
    msgs = task_obj.bug.messages
    last = len(msgs)
    start = max(0, last - last_messages_considered)

    for msg in msgs[start:last]:
        try:
            activity_list.append((msg.date_created, msg.owner.self_link))
        except ClientError as exc:
            if exc.response["status"] == "410":
                continue
            raise
    if not activity_list:
        return False
    most_recent = activity_list[-1]
    threshold = most_recent[0] - timedelta(hours=1)
    recent = [most_recent]
    for item in reversed(activity_list[:-1]):
        if item[0] < threshold:
            break
        recent.append(item)
    return all(a[1] in activity_subscriber_links for a in recent)


async def fetch_unapproved_bugs_for_series(
    session: aiohttp.ClientSession, changes_urls: list[tuple[str, str]]
) -> dict[str, set[str]]:
    """Return {source_package: {bug_number, ...}} for a batch of (pkg, changes_url) pairs.

    Takes pre-collected (pkg_name, changes_file_url) string pairs - no LP objects.
    """

    async def _bugs_for_upload(pkg: str, url: str) -> tuple[str, list[str]]:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return pkg, []
                text = await resp.text()
                changes = debian.deb822.Changes(text)
                bugs_str = changes.get("Launchpad-Bugs-Fixed", "")
                return pkg, bugs_str.split()
        except Exception as exc:
            logging.debug("Error fetching changes %s: %s", url, exc)
            return pkg, []

    results = await asyncio.gather(*[_bugs_for_upload(pkg, url) for pkg, url in changes_urls])
    pkg_bugs: dict[str, set[str]] = {}
    for pkg, bugs in results:
        pkg_bugs.setdefault(pkg, set()).update(bugs)
    return pkg_bugs


def fetch_bugs(
    lp: Launchpad,
    team_config: TeamConfig,
    filter: TaskFilterOptions,
    mode: FetchMode,
    update_filter: str | None,
    expire_level1_days: int = 60,
    expire_level2_days: int = 180,
) -> LaunchpadTasks:
    """Synchronous LP fetch - run inside asyncio.to_thread().

    All LP object access stays in this function; only plain data and Task
    objects leave (Task objects hold LP objects but are only rendered after
    the thread completes, never concurrently).
    """

    ubuntu = lp.distributions["Ubuntu"]
    team = lp.people[team_config.lp_team]

    activity_people = lp.people[team_config.lp_team].participants
    activity_links = {p.self_link for p in activity_people}

    match mode:
        case FetchMode.triage:
            bugs_start = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=filter.start,
                    structural_subscriber=team,
                    status=POSSIBLE_BUG_STATUSES,
                )
            }
            bugs_end = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=filter.end,
                    structural_subscriber=team,
                    status=POSSIBLE_BUG_STATUSES,
                )
            }
            already_subscribed = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=filter.start,
                    structural_subscriber=team,
                    bug_subscriber=team,
                    status=POSSIBLE_BUG_STATUSES,
                )
            }
            bugs_in_range = {k: v for k, v in bugs_start.items() if k not in bugs_end}

        case FetchMode.todo:
            bugs_in_range = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    bug_subscriber=team,
                    tags=[team_config.lp_todo_tag, "-bot-stop-nagging"],
                    tags_combinator="All",
                    status=TRACKED_BUG_STATUSES,
                )
            }
            already_subscribed = {}

        case FetchMode.subscribed:
            bugs_in_range = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    bug_subscriber=team,
                    tags=["-bot-stop-nagging", f"-{team_config.lp_todo_tag}"],
                    tags_combinator="All",
                    status=OPEN_BUG_STATUSES,
                )
            }
            already_subscribed = bugs_in_range

        case _:
            raise ValueError(f"Unknown fetch mode: {mode!r}")

    tasks = set()
    for (bug_link, _), lp_task in bugs_in_range.items():
        src = _fast_target_name(lp_task)
        if src in team_config.lp_ignore_packages:
            continue
        is_subscribed = (bug_link, src) in already_subscribed
        is_ours = _last_activity_ours(lp_task, activity_links)

        # Apply update filter (triage mode only)
        if mode == FetchMode.triage and update_filter:
            if update_filter == "theirs" and is_ours:
                continue
            if update_filter == "ours" and not is_ours:
                continue

        task = Task(
            lp_task,
            subscribed=is_subscribed,
            last_activity_ours=is_ours,
        )
        tasks.add(task)

    # Expiration section: bugs that fell through the triage window N days ago.
    # Uses the same shifted-window set-difference pattern as the main triage query.
    expiring_tagged: list[Task] = []
    expiring_subscribed: list[Task] = []
    if mode == FetchMode.triage and filter.show_expiration and filter.start and filter.end:

        def _expiring_window(days: int) -> list[Task]:
            shift = timedelta(days=days)
            w_start = filter.start - shift
            w_end = filter.end - shift

            since_start = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=w_start,
                    structural_subscriber=team,
                    status=OPEN_BUG_STATUSES,
                )
            }
            since_end = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=w_end,
                    structural_subscriber=team,
                    status=OPEN_BUG_STATUSES,
                )
            }
            result = []
            for key, lp_task in since_start.items():
                if key in since_end:
                    continue
                src = _fast_target_name(lp_task)
                if src in team_config.lp_ignore_packages:
                    continue
                is_ours = _last_activity_ours(lp_task, activity_links)
                result.append(Task(lp_task, subscribed=True, last_activity_ours=is_ours, expiring=True))
            return result

        logging.info("Fetching expiring bugs level 1 (~%d days ago)\u2026", expire_level1_days)
        expiring_tagged = _expiring_window(expire_level1_days)
        logging.info("Launchpad: %d expiring level-1 bugs.", len({t.number for t in expiring_tagged}))

        logging.info("Fetching expiring bugs level 2 (~%d days ago)\u2026", expire_level2_days)
        expiring_subscribed = _expiring_window(expire_level2_days)
        logging.info("Launchpad: %d expiring level-2 bugs.", len({t.number for t in expiring_subscribed}))

    active_series = [s.name for s in ubuntu.series_collection if s.active]

    # Collect (pkg_name, changes_url) pairs for all active series - all LP access here,
    # so no LP objects escape to the async event loop
    changes_pairs: list[tuple[str, str]] = []
    for series_name in active_series:
        series_obj = ubuntu.getSeries(name_or_version=series_name)
        uploads = list(series_obj.getPackageUploads(pocket="Proposed", status="Unapproved"))
        for upload in uploads:
            url = upload.changes_file_url
            if url:
                changes_pairs.append((upload.package_name, str(url)))

    return LaunchpadTasks(
        list(tasks),
        lp,
        changes_pairs,
        NOWORK_BUG_STATUSES,
        OPEN_BUG_STATUSES,
        expiring_tagged,
        expiring_subscribed,
    )
