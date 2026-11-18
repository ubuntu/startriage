"""Launchpad bug fetcher for startriage."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import debian.deb822
import platformdirs
from launchpadlib import __version__ as launchpadlib_version
from launchpadlib.credentials import (
    AuthorizeRequestTokenWithURL,
    Credentials,
    UnencryptedFileCredentialStore,
)
from launchpadlib.launchpad import Launchpad

from startriage.source import TaskFilterOptions

from ...config import TeamConfig
from ...source import FetchMode
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


LP_CONSUMER_NAME = "startriage"

# startriage only ever reads from Launchpad, so we offer the end-user just the
# read-only access levels in the authorization web UI.
LP_ACCESS_LEVELS = ["READ_PUBLIC", "READ_PRIVATE"]

# launchpadlib >= 2.2.0 refuses cached credentials of other consumers itself;
# distro packages (e.g. the snap's stage-packages) may still ship older ones.
_LP_REUSES_FOREIGN_CREDENTIALS = tuple(int(part) for part in launchpadlib_version.split(".")[:2]) < (2, 2)


def _discard_foreign_credentials(cred_location: Path) -> None:
    """Drop a cached credential that belongs to a different OAuth consumer.

    launchpadlib < 2.2.0's UnencryptedFileCredentialStore loads whatever consumer
    is in the file, ignoring the consumer we actually ask for. Older startriage
    versions logged in with a system-wide desktop consumer (keyed by hostname),
    so a leftover file makes the first API call return 401 and triggers a broken
    mid-session re-auth against the wrong consumer ("Not allowed here" on the
    Launchpad authorization page). Discard such files so we re-authenticate
    cleanly as LP_CONSUMER_NAME.
    """
    if not cred_location.exists():
        return
    creds = Credentials.load_from_path(str(cred_location))
    consumer_key = getattr(creds.consumer, "key", None)
    if consumer_key != LP_CONSUMER_NAME:
        logger.info(
            "discarding launchpad credentials for foreign consumer %r (expected %r)",
            consumer_key,
            LP_CONSUMER_NAME,
        )
        cred_location.unlink(missing_ok=True)


class LaunchpadAuthError(Exception):
    """Launchpad credentials are missing or were rejected."""


class _NoAuthorization(AuthorizeRequestTokenWithURL):
    """Fail instead of prompting; only `startriage login launchpad` authorizes."""

    def make_end_user_authorize_token(self, credentials, request_token):
        raise LaunchpadAuthError(
            "Launchpad credentials are missing or were rejected; run 'startriage login launchpad'"
        )


def connect_launchpad(credentials_file: Path | None = None, interactive: bool = False) -> Launchpad:
    """Log into Launchpad with stored credentials; *interactive* prompts on the terminal if needed."""
    if credentials_file is None:
        credentials_file = platformdirs.user_data_path("startriage") / "lp_creds"
    credentials_file.parent.mkdir(parents=True, exist_ok=True)
    if _LP_REUSES_FOREIGN_CREDENTIALS:
        _discard_foreign_credentials(credentials_file)
    credential_store = UnencryptedFileCredentialStore(str(credentials_file))

    # an explicit engine prints the authorization URL instead of opening a browser,
    # and makes launchpadlib < 2.2.0 honor our consumer name and access levels
    engine_cls = AuthorizeRequestTokenWithURL if interactive else _NoAuthorization

    logger.debug("logging into launchpad...")
    return Launchpad.login_with(
        consumer_name=LP_CONSUMER_NAME,
        service_root="production",
        version="devel",
        credential_store=credential_store,
        authorization_engine=engine_cls(
            "production", consumer_name=LP_CONSUMER_NAME, allow_access_levels=LP_ACCESS_LEVELS
        ),
    )


def login_launchpad(credentials_file: Path | None = None) -> str:
    """Authorize startriage on the terminal unless stored credentials work; return the user name."""
    # the first request triggers authorization for missing or rejected credentials
    return connect_launchpad(credentials_file, interactive=True).me.name


def _fast_target_name(obj) -> str:
    return obj.target_link.split("/")[-1]


def _search_tasks_all_series(distro, **kwargs) -> dict[tuple[str, str], object]:
    """Search tasks across all active series (LP #314432 workaround), keyed by (bug_link, target)."""
    result = {(task.bug_link, _fast_target_name(task)): task for task in distro.searchTasks(**kwargs)}
    for series in distro.series_collection:
        if not series.active:
            continue
        result.update(
            {(task.bug_link, _fast_target_name(task)): task for task in series.searchTasks(**kwargs)}
        )
    return result


def _modified_in(start: datetime, end: datetime) -> dict[str, datetime]:
    """searchTasks arguments for the half-open interval [start, end)."""
    # Launchpad compares `> modified_since`, at microsecond precision
    return {"modified_since": start - timedelta(microseconds=1), "modified_before": end}


def _search_team_tasks(distro, team, **kwargs) -> tuple[dict[tuple[str, str], object], set[tuple[str, str]]]:
    """Search tasks the team is subscribed to, structurally or directly; also return the direct ones' keys."""
    # searchTasks ANDs structural_subscriber and bug_subscriber, so OR them here
    direct = _search_tasks_all_series(distro, bug_subscriber=team, **kwargs)
    return _search_tasks_all_series(distro, structural_subscriber=team, **kwargs) | direct, set(direct)


# Per-user events that are no triage activity, as regexes fully matching (whatchanged, new value);
# comments are ("comment", content). Other events from the same account still count.
_LP_IGNORE_EVENTS: dict[str, list[tuple[str, str]]] = {
    "janitor": [
        ("merge proposal linked", ".*"),
        ("merge proposal unlinked", ".*"),
        # closing a bug fixed by an upload
        (".*: status", "Fix Released"),
        ("comment", "This bug was fixed in the package .*"),
        # expiring stale Incomplete bugs
        (".*: status", "Expired"),
        ("comment", r"\[Expired for .* because there has been no activity for \d+ days\.\]"),
    ],
}


def _ignored(person_link: str | None, whatchanged: str, value: str | None) -> bool:
    name = person_link.rpartition("/~")[2] if person_link else None
    return any(
        re.fullmatch(what, whatchanged, re.DOTALL) and re.fullmatch(pattern, value or "", re.DOTALL)
        for what, pattern in _LP_IGNORE_EVENTS.get(name or "", ())
    )


def _last_relevant(entries, event) -> tuple[datetime, str | None] | None:
    """Return (date, person_link) of the last entry that isn't ignored; event() unpacks an entry."""
    for i in range(len(entries) - 1, -1, -1):
        dt, person_link, whatchanged, value = event(entries[i])
        if not _ignored(person_link, whatchanged, value):
            return dt, person_link
    return None


def _last_activity(
    cache: dict[str, tuple[datetime | None, bool]],
    src_pkg: str,
    bug_link: str,
    lp_task,
    lp_user_links: set[str],
) -> tuple[datetime | None, bool]:
    if bug_link not in cache:
        logger.debug("determining last activity for pkg %s bug %s", src_pkg, bug_link)
        cache[bug_link] = _fetch_last_activity(lp_task, lp_user_links)
    else:
        logger.debug("last activity for pkg %s bug %s is cached", src_pkg, bug_link)
    logger.debug("last activity for pkg %s bug %s (date, ours): %s", src_pkg, bug_link, cache[bug_link])
    return cache[bug_link]


def _fetch_last_activity(task_obj, lp_user_links: set[str]) -> tuple[datetime | None, bool]:
    """Return the date of the most recent relevant bug event, and whether a team member caused it.

    Considers both comments (bug.messages) and state/metadata changes
    (bug.activity) - LP provides no unified timeline, so the last item of
    each collection is compared and the more recent one wins.

    Events in _LP_IGNORE_EVENTS are skipped: bot follow-ups neither hide who acted last
    nor make a bug look changed.
    None person_link means an automated/system change — treated as external.
    """
    bug = task_obj.bug
    candidates = [
        c
        for c in (
            _last_relevant(bug.messages, lambda m: (m.date_created, m.owner_link, "comment", m.content)),
            _last_relevant(bug.activity, lambda a: (a.datechanged, a.person_link, a.whatchanged, a.newvalue)),
        )
        if c is not None
    ]
    if not candidates:
        return None, False

    dt, person_link = max(candidates, key=lambda e: e[0])
    return dt, person_link is not None and person_link in lp_user_links


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
            logger.debug("fetching tasks modified in %s..%s...", filter.start, filter.end)
            bugs_in_range, direct_subscriptions = _search_team_tasks(
                ubuntu,
                team,
                **_modified_in(filter.start, filter.end),
                status=POSSIBLE_BUG_STATUSES,
            )

        case FetchMode.todo:
            # A bug tagged both todo and freezer counts as todo (actionable).
            tags = [team_config.lp_todo_tag]
            if team_config.lp_freezer_tag:
                tags.append(team_config.lp_freezer_tag)
            logger.debug("fetching todo tasks (tags %s)...", tags)
            bugs_in_range = _search_tasks_all_series(
                ubuntu,
                tags=tags,
                tags_combinator="Any",
                status=TRACKED_BUG_STATUSES,
            )
            direct_subscriptions = set()

        case FetchMode.subscribed:
            logger.debug("fetching subscribed tasks (subscriber = %s)...", team)
            bugs_in_range = _search_tasks_all_series(
                ubuntu,
                bug_subscriber=team,
                tags=["-bot-stop-nagging", f"-{team_config.lp_todo_tag}"],
                tags_combinator="All",
                status=OPEN_BUG_STATUSES,
            )
            direct_subscriptions = set(bugs_in_range)

        case _:
            raise ValueError(f"Unknown fetch mode: {mode!r}")

    tasks = set()
    freezer_tasks: list[Task] = []
    last_activity_cache: dict[str, tuple[datetime | None, bool]] = {}
    for (bug_link, _), lp_task in bugs_in_range.items():
        src = _fast_target_name(lp_task)
        if src in team_config.lp_ignore_packages:
            continue
        last_date, is_ours = _last_activity(last_activity_cache, src, bug_link, lp_task, team_user_links)

        # no relevant activity in the window: nothing to triage
        if mode == FetchMode.triage and (last_date is None or last_date < filter.start):
            continue

        # Apply update filter (triage mode only)
        if mode == FetchMode.triage and update_filter:
            if update_filter == "theirs" and is_ours:
                continue
            if update_filter == "ours" and not is_ours:
                continue

        task = Task(
            lp_task,
            last_activity_ours=is_ours,
            direct_subscription=(bug_link, src) in direct_subscriptions,
        )
        logger.debug("listing bug task %s for pkg %s bug %s", task, src, bug_link)
        if (
            mode == FetchMode.todo
            and team_config.lp_freezer_tag
            and team_config.lp_freezer_tag in task.tags
            and team_config.lp_todo_tag not in task.tags
        ):
            # freezer tasks: watched but not actionable yet
            freezer_tasks.append(task)
        else:
            tasks.add(task)

    # Expiration section: bugs last touched in the triage window shifted N days back.
    expire_level1: list[Task] = []
    expire_level2: list[Task] = []
    if mode == FetchMode.triage and filter.show_expiration and filter.start and filter.end:

        def _expiring_window(days: int) -> list[Task]:
            shift = timedelta(days=days)
            in_window, direct = _search_team_tasks(
                ubuntu,
                team,
                **_modified_in(filter.start - shift, filter.end - shift),
                status=OPEN_BUG_STATUSES,
            )
            result = []
            for bug_ref, lp_task in in_window.items():
                src = _fast_target_name(lp_task)
                if src in team_config.lp_ignore_packages:
                    continue
                _, is_ours = _last_activity(last_activity_cache, src, bug_ref[0], lp_task, team_user_links)
                result.append(
                    Task(
                        lp_task,
                        last_activity_ours=is_ours,
                        direct_subscription=bug_ref in direct,
                        expiring=True,
                    )
                )
            return result

        logger.debug("fetching expiring bugs level 1 (~%d days ago)\u2026", expire_level1_days)
        expire_level1 = _expiring_window(expire_level1_days)
        logger.debug("%d expiring level-1 bugs.", len({t.number for t in expire_level1}))

        logger.debug("fetching expiring bugs level 2 (~%d days ago)\u2026", expire_level2_days)
        expire_level2 = _expiring_window(expire_level2_days)
        logger.debug("%d expiring level-2 bugs.", len({t.number for t in expire_level2}))

    active_series = [s.name for s in ubuntu.series_collection if s.active]
    logger.debug("determining unapproved uploads for bug (%d release series)...", len(active_series))

    relevant_packages = {t.src for t in tasks}
    relevant_packages.update(t.src for t in expire_level1)
    relevant_packages.update(t.src for t in expire_level2)

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
        expire_level1,
        expire_level2,
        freezer_tasks,
    )
