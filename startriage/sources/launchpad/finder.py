"""Launchpad bug fetcher for startriage."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
import debian.deb822
import platformdirs
from launchpadlib.credentials import AuthorizeRequestTokenWithURL, UnencryptedFileCredentialStore
from launchpadlib.launchpad import Launchpad
from lazr.restfulclient.errors import ClientError

from startriage.source import TaskFilterOptions

from ...config import TeamConfig
from ...enums import FetchMode
from .models import LaunchpadTasks, Task

logger = logging.getLogger(__name__)


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

    logger.debug("logging into launchpad...")
    return Launchpad.login_with(
        consumer_name="startriage",
        service_root="production",
        version="devel",
        credential_store=credential_store,
        # workaround until https://code.launchpad.net/~jj/launchpadlib/+git/launchpadlib/+merge/505695
        # is released
        authorization_engine=AuthorizeRequestTokenWithURL("production", consumer_name="startriage"),
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


# Per-user events to ignore when computing last activity.
# A bot listed here is only ignored for the specific whatchanged values given;
# other events from the same account are still considered.
_LP_IGNORE_EVENTS: dict[str, frozenset[str]] = {
    "janitor": frozenset({"merge proposal linked"}),
}


def _last_activity_ours(
    bugs_touched_by_us_last: dict[str, bool], src_pkg: str, bug_link: str, lp_task, lp_user_links: set[str]
) -> bool:
    if bug_link not in bugs_touched_by_us_last:
        logger.debug("determining last activity for pkg %s bug %s", src_pkg, bug_link)
        is_ours = _fetch_last_activity_ours(lp_task, lp_user_links)
        logger.debug("last activity for pkg %s bug %s is ours: %s", src_pkg, bug_link, is_ours)
        bugs_touched_by_us_last[bug_link] = is_ours
    else:
        is_ours = bugs_touched_by_us_last[bug_link]
        logger.debug("last activity for pkg %s bug %s is ours: %s (cached)", src_pkg, bug_link, is_ours)

    return is_ours


def _fetch_last_activity_ours(task_obj, lp_user_links: set[str]) -> bool:
    """Return True if the single most recent human bug event was by a team member.

    Considers both comments (bug.messages) and state/metadata changes
    (bug.activity) - LP provides no unified timeline, so the last item of
    each collection is compared and the more recent one wins.

    Events listed in _LP_IGNORE_EVENTS for a given user are skipped so that
    automated follow-ups to team actions do not hide our work.
    None person_link means an automated/system change — treated as external.
    """
    if not lp_user_links:
        return False

    candidates: list[tuple[datetime, str | None]] = []

    def _try_append(dt, person_obj, whatchanged: str | None = None) -> None:
        try:
            name = person_obj.name if person_obj else None
            ignore = _LP_IGNORE_EVENTS.get(name) if name else None
            if ignore and whatchanged and whatchanged in ignore:
                return
            candidates.append((dt, person_obj.self_link if person_obj else None))
        except ClientError as exc:
            if exc.response["status"] != "410":  # user deleted
                raise

    msgs = task_obj.bug.messages
    n_msgs = len(msgs)
    if n_msgs > 0:
        msg = msgs[n_msgs - 1]
        _try_append(msg.date_created, msg.owner)

    activity = task_obj.bug.activity
    n_act = len(activity)
    if n_act > 0:
        entry = activity[n_act - 1]
        _try_append(entry.datechanged, entry.person, entry.whatchanged)

    if not candidates:
        return False

    _, last_person_link = max(candidates, key=lambda e: e[0])
    return last_person_link is not None and last_person_link in lp_user_links


async def fetch_changelogs(
    session: aiohttp.ClientSession, changes_urls: list[tuple[str, str]]
) -> dict[str, set[str]]:
    """Return {source_package: {bug_number, ...}} for a batch of (pkg, changes_url) pairs.

    Takes pre-collected (pkg_name, changes_file_url) string pairs - no LP objects.
    """

    logger.debug("fetching changes %d changelogs", len(changes_urls))

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
            logger.debug("Error fetching changes %s: %s", url, exc)
            return pkg, []

    results = await asyncio.gather(*[_bugs_for_upload(pkg, url) for pkg, url in changes_urls])
    pkg_bugs: dict[str, set[str]] = {}
    for pkg, bugs in results:
        pkg_bugs.setdefault(pkg, set()).update(bugs)
        logger.debug("changelog for pkg %s fixes %d bugs", pkg, len(bugs))
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
    team_user_links = {p.self_link for p in activity_people}

    match mode:
        case FetchMode.triage:
            logger.debug("fetching tasks since start %s...", filter.start)
            bugs_start = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=filter.start,
                    structural_subscriber=team,
                    status=POSSIBLE_BUG_STATUSES,
                )
            }
            logger.debug("fetching tasks since end %s...", filter.end)
            bugs_end = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    modified_since=filter.end,
                    structural_subscriber=team,
                    status=POSSIBLE_BUG_STATUSES,
                )
            }
            logger.debug("fetching subscribed tasks since start %s...", filter.start)
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
            logger.debug("intersecting tasks %d start from %d end tasks", len(bugs_start), len(bugs_end))
            bugs_in_range = {k: v for k, v in bugs_start.items() if k not in bugs_end}

        case FetchMode.todo:
            logger.debug("fetching todo tasks (tag %s)...", team_config.lp_todo_tag)
            bugs_in_range = {
                (t.bug_link, _fast_target_name(t)): t
                for t in _search_tasks_all_series(
                    ubuntu,
                    tags=[team_config.lp_todo_tag, "-bot-stop-nagging"],
                    tags_combinator="All",
                    status=TRACKED_BUG_STATUSES,
                )
            }
            already_subscribed = {}

        case FetchMode.subscribed:
            logger.debug("fetching subscribed tasks (subscriber = %s)...", team)
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
    bugs_touched_by_us_last: dict[str, bool] = {}
    for (bug_link, _), lp_task in bugs_in_range.items():
        src = _fast_target_name(lp_task)
        if src in team_config.lp_ignore_packages:
            continue
        is_subscribed = (bug_link, src) in already_subscribed

        is_ours = _last_activity_ours(bugs_touched_by_us_last, src, bug_link, lp_task, team_user_links)

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
        logger.debug("listing bug task %s for pkg %s bug %s", task, src, bug_link)
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
            for bug_ref, lp_task in since_start.items():
                if bug_ref in since_end:
                    continue
                src = _fast_target_name(lp_task)
                if src in team_config.lp_ignore_packages:
                    continue
                is_ours = _last_activity_ours(
                    bugs_touched_by_us_last, src, bug_ref[0], lp_task, team_user_links
                )
                result.append(Task(lp_task, subscribed=True, last_activity_ours=is_ours, expiring=True))
            return result

        logger.debug("fetching expiring bugs level 1 (~%d days ago)\u2026", expire_level1_days)
        expiring_tagged = _expiring_window(expire_level1_days)
        logger.debug("%d expiring level-1 bugs.", len({t.number for t in expiring_tagged}))

        logger.debug("fetching expiring bugs level 2 (~%d days ago)\u2026", expire_level2_days)
        expiring_subscribed = _expiring_window(expire_level2_days)
        logger.debug("%d expiring level-2 bugs.", len({t.number for t in expiring_subscribed}))

    active_series = [s.name for s in ubuntu.series_collection if s.active]
    logger.debug("determining unapproved uploads for bug (%d release series)...", len(active_series))

    relevant_packages = {t.src for t in tasks}
    relevant_packages.update(t.src for t in expiring_tagged)
    relevant_packages.update(t.src for t in expiring_subscribed)

    # Collect (pkg_name, changes_url) pairs for all active series - all LP access here,
    # so no LP objects escape to the async event loop
    changes_pairs: list[tuple[str, str]] = []
    if relevant_packages:
        for series_name in active_series:
            logger.debug("fetching unapproved uploads in series %s...", series_name)
            series_obj = ubuntu.getSeries(name_or_version=series_name)
            uploads = list(series_obj.getPackageUploads(pocket="Proposed", status="Unapproved"))
            for upload in uploads:
                if upload.package_name not in relevant_packages:
                    continue
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
