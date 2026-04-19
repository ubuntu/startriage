"""Launchpad bug fetcher for startriage.

Performance improvements over ustriage:
1. _sibling_tasks is @lru_cache'd on the Task - computed once per task.
2. Unapproved-queue check is done in bulk: one getPackageUploads() call
   per active series (not per bug), then we match bugs against that set.
   This is the main source of the old 30-minute runtime.
3. Changelog URL fetches (for unapproved matching) are done concurrently
   via asyncio.gather() + aiohttp after the LP query returns.
4. last_activity_ours uses only the last 3 messages (same as ustriage),
   keeping per-bug API calls minimal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .triage import LaunchpadTriage

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import aiohttp
from launchpadlib.credentials import UnencryptedFileCredentialStore
from launchpadlib.launchpad import Launchpad

from startriage.config import GeneralConfig, TeamConfig

from .models import Task

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
    cred_location = os.path.expanduser("~/.lp_creds")
    credential_store = UnencryptedFileCredentialStore(cred_location)
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


def _last_activity_ours(task_obj, activity_subscriber_links: set[str]) -> bool:
    if not activity_subscriber_links:
        return False
    activity_list = []
    msgs = task_obj.bug.messages
    last = len(msgs)
    start = max(0, last - 3)
    from lazr.restfulclient.errors import ClientError

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


async def _fetch_unapproved_bugs_for_series(
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
                import debian.deb822

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


def _sync_fetch_bugs(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    start_date: date | None,
    end_date: date | None,
    mode: Literal["triage", "todo", "subscribed"],
) -> tuple[list[Task], list[tuple[str, str]]]:  # tasks, [(pkg, changes_url), ...]
    """Synchronous LP fetch - run inside asyncio.to_thread().

    Returns tasks plus (pkg_name, changes_url) string pairs for unapproved-queue
    checking. All LP object access stays in this function; only plain data leaves.
    """
    lp = connect_launchpad()
    Task.LP = lp
    Task.NOWORK_BUG_STATUSES = NOWORK_BUG_STATUSES
    Task.OPEN_BUG_STATUSES = OPEN_BUG_STATUSES

    ubuntu = lp.distributions["Ubuntu"]
    team = lp.people[team_config.lp_team]

    try:
        activity_people = lp.people[team_config.lp_team].participants
        activity_links = {p.self_link for p in activity_people}
    except Exception:
        activity_links = set()

    start_dt = (
        datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc) if start_date else None
    )
    end_dt = (
        datetime.combine(end_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc)
        if end_date
        else None
    )

    if mode == "triage":
        bugs_start = {
            (t.bug_link, _fast_target_name(t)): t
            for t in _search_tasks_all_series(
                ubuntu,
                modified_since=start_dt,
                structural_subscriber=team,
                status=POSSIBLE_BUG_STATUSES,
            )
        }
        bugs_end = {
            (t.bug_link, _fast_target_name(t)): t
            for t in _search_tasks_all_series(
                ubuntu,
                modified_since=end_dt,
                structural_subscriber=team,
                status=POSSIBLE_BUG_STATUSES,
            )
        }
        already_subscribed = {
            (t.bug_link, _fast_target_name(t)): t
            for t in _search_tasks_all_series(
                ubuntu,
                modified_since=start_dt,
                structural_subscriber=team,
                bug_subscriber=team,
                status=POSSIBLE_BUG_STATUSES,
            )
        }
        bugs_in_range = {k: v for k, v in bugs_start.items() if k not in bugs_end}

    elif mode == "todo":
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

    else:  # subscribed
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

    tasks = set()
    for (bug_link, _), lp_task in bugs_in_range.items():
        src = _fast_target_name(lp_task)
        if src in team_config.lp_ignore_packages:
            continue
        is_subscribed = (bug_link, src) in already_subscribed
        is_ours = _last_activity_ours(lp_task, activity_links)

        # Apply update filter (triage mode only)
        if mode == "triage":
            update_filter = general_config.lp_update_filter
            if update_filter == "theirs" and is_ours:
                continue
            if update_filter == "ours" and not is_ours:
                continue

        task = Task.create_from_launchpadlib_object(
            lp_task,
            subscribed=is_subscribed,
            last_activity_ours=is_ours,
        )
        tasks.add(task)

    active_series = [s.name for s in ubuntu.series_collection if s.active]

    # Collect (pkg_name, changes_url) pairs for all active series - all LP access here,
    # so no LP objects escape to the async event loop
    changes_pairs: list[tuple[str, str]] = []
    for series_name in active_series:
        try:
            series_obj = ubuntu.getSeries(name_or_version=series_name)
            uploads = list(series_obj.getPackageUploads(pocket="Proposed", status="Unapproved"))
            for upload in uploads:
                url = upload.changes_file_url
                if url:
                    changes_pairs.append((upload.package_name, str(url)))
        except Exception as exc:
            logging.debug("Error collecting unapproved uploads for %s: %s", series_name, exc)

    return list(tasks), changes_pairs


async def fetch_bugs(
    team_config: TeamConfig,
    general_config: GeneralConfig,
    start_date: date | None,
    end_date: date | None,
    mode: Literal["triage", "todo", "subscribed"] = "triage",
) -> "LaunchpadTriage":
    """Fetch Launchpad bugs, then bulk-check unapproved queue concurrently."""
    from .triage import LaunchpadTriage  # avoid circular at module load

    logging.info("Fetching Launchpad bugs (this may take a while)…")
    tasks, changes_pairs = await asyncio.to_thread(
        _sync_fetch_bugs, team_config, general_config, start_date, end_date, mode
    )
    logging.info("Launchpad: %d bugs fetched. Checking unapproved queue…", len(tasks))

    # Bulk unapproved check: all .changes file fetches are concurrent via aiohttp.
    # No LP objects are used here - only plain (pkg, url) string pairs.
    async with aiohttp.ClientSession() as session:
        pkg_bugs = await _fetch_unapproved_bugs_for_series(session, changes_pairs)

    Task._unapproved_cache = {}
    for pkg, bug_nums in pkg_bugs.items():
        for bug_num in bug_nums:
            Task._unapproved_cache[(bug_num, pkg)] = True

    triage = LaunchpadTriage(
        tasks=tasks,
        start=start_date,
        end=end_date,
        team_config=team_config,
        general_config=general_config,
        mode=mode,
    )
    logging.info("Launchpad: done.")
    return triage
