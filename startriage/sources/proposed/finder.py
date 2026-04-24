"""Fetch and parse the proposed-migration YAML"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import yaml

from .models import MigrationExcuse, ProposedMigrationData

_URL = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses_by_team.yaml"
_HTML_URL = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses_by_team.html"

_GENERATED_RE = re.compile(r"Generated:\s+(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s+UTC")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YAML safety: the file uses !!python/object/apply:collections.defaultdict
# for the per-package data dicts.  Teach SafeLoader to treat those as plain
# dicts using their 'dictitems' mapping so we never exec arbitrary Python.
# ---------------------------------------------------------------------------

def _make_safe_loader() -> type[yaml.SafeLoader]:
    class _Loader(yaml.SafeLoader):
        pass

    def _python_object_apply(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> dict:
        mapping = loader.construct_mapping(node, deep=True)
        return mapping.get("dictitems", {})

    def _python_name(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> None:
        # Used as the defaultdict factory anchor; we don't need the type itself.
        return None

    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/object/apply:",
        _python_object_apply,
    )
    _Loader.add_multi_constructor(
        "tag:yaml.org,2002:python/name:",
        _python_name,
    )
    return _Loader


_SafeLoader = _make_safe_loader()


def _parse_excuse(item: dict, generated_date: datetime) -> MigrationExcuse | None:
    """Parse one list entry from a team section into a MigrationExcuse."""
    kind = item.get("kind")
    if kind not in ("package-in-proposed", "regressing-other"):
        return None

    package = item.get("package_in_proposed", "")
    if not package:
        return None

    age_days: float = float(item.get("age", 0.0))
    in_proposed_since = generated_date - timedelta(days=age_days)

    # The per-package metadata lives in the 'data' key which is parsed from
    # a Python defaultdict YAML tag — our custom constructor converts it to
    # a plain dict containing what was originally 'dictitems'.
    data: dict = item.get("data") or {}

    old_version: str = str(data.get("old-version", "-"))
    new_version: str = str(data.get("new-version", ""))

    reasons: list[str] = list(data.get("reason") or [])

    # update-excuse maps LP bug-ID strings to timestamps; lives under policy_info
    policy_info: dict = data.get("policy_info") or {}
    update_excuse: dict = policy_info.get("update-excuse") or {}
    bugs: list[int] = []
    for key in update_excuse:
        try:
            bugs.append(int(key))
        except (ValueError, TypeError):
            pass

    is_candidate: bool = data["is-candidate"]

    return MigrationExcuse(
        package=package,
        old_version=old_version,
        new_version=new_version,
        in_proposed_since=in_proposed_since,
        is_candidate=is_candidate,
        reasons=reasons,
        bugs=sorted(bugs),
    )


async def _fetch_yaml(session: aiohttp.ClientSession) -> bytes:
    async with session.get(_URL) as resp:
        resp.raise_for_status()
        return await resp.read()


async def _fetch_generated_date_from_html(session: aiohttp.ClientSession) -> datetime | None:
    """Scrape the generation timestamp from the first few KB of the HTML page."""
    try:
        async with session.get(_HTML_URL) as resp:
            resp.raise_for_status()
            chunk = await resp.content.read(2048)
        text = chunk.decode("utf-8", errors="replace")
        m = _GENERATED_RE.search(text)
        if m:
            return datetime.strptime(m.group(1), "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        log.debug("Could not fetch generated date from HTML", exc_info=True)
    return None


async def fetch_proposed_migration(
    teams: list[str],
    min_age: int,
    session: aiohttp.ClientSession,
) -> ProposedMigrationData:
    """Download and parse update_excuses_by_team.yaml, filtering by team and age."""
    log.debug("Fetching proposed migration data from %s", _URL)
    generated_date, raw_bytes = await asyncio.gather(
        _fetch_generated_date_from_html(session),
        _fetch_yaml(session),
    )

    parsed = yaml.load(raw_bytes, Loader=_SafeLoader)

    if not isinstance(parsed, dict):
        raise ValueError("Unexpected format for proposed migration YAML: expected a dict at the top level")

    now = datetime.now(timezone.utc)
    ref_date = generated_date or now
    cutoff = ref_date - timedelta(days=min_age)

    excuses: list[MigrationExcuse] = []
    if isinstance(parsed, dict):
        for team in teams:
            items = parsed.get(team) or []
            if not isinstance(items, list):
                log.warning("Unexpected type for team %r in proposed migration YAML", team)
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                excuse = _parse_excuse(item, ref_date)
                if excuse is not None and excuse.in_proposed_since <= cutoff:
                    excuses.append(excuse)

    # deduplicate by package name (keep oldest entry if the same package appears
    # under multiple teams)
    seen: dict[str, MigrationExcuse] = {}
    for exc in excuses:
        if exc.package not in seen or exc.in_proposed_since < seen[exc.package].in_proposed_since:
            seen[exc.package] = exc
    excuses = sorted(seen.values(), key=lambda e: (e.in_proposed_since, e.package))

    return ProposedMigrationData(generated_date=generated_date, excuses=excuses)
