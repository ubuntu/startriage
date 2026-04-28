"""Proposed migration triage result"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from ...config import StarTriageConfig
from ...enums import FetchMode
from ...output import OutputConfig, OutputFormat, TriageResult, hyperlink, truncate_string
from ...savebugs import BugPersistor
from ...source import TaskFilterOptions
from .finder import fetch_proposed_migration
from .models import MigrationExcuse, ProposedMigrationData

_LP_SOURCE_URL = "https://launchpad.net/ubuntu/+source/{pkg}"
_LP_SOURCE_VERSION_URL = "https://launchpad.net/ubuntu/+source/{pkg}/{version}"
_LP_BUG_URL = "https://bugs.launchpad.net/bugs/{bug}"
_EXCUSES_URL = "https://ubuntu-archive-team.ubuntu.com/proposed-migration/update_excuses.html#{pkg}"

# ANSI colours (applied unconditionally, same pattern as launchpad/models.py)
_COLOR_GREEN = "\033[0;32m"
_COLOR_YELLOW = "\033[0;33m"
_COLOR_RED = "\033[0;31m"
_COLOR_RESET = "\033[0m"
_BOX = "\u25a0"  # ■ BLACK SQUARE


def _pkg_link(pkg: str, fmt: OutputFormat) -> str:
    return hyperlink(_LP_SOURCE_URL.format(pkg=pkg), pkg, fmt)


def _version_link(pkg: str, version: str, fmt: OutputFormat) -> str:
    if version == "-":
        return version
    return hyperlink(_LP_SOURCE_VERSION_URL.format(pkg=pkg, version=version), version, fmt)


def _bug_link(bug_id: int, fmt: OutputFormat) -> str:
    return hyperlink(_LP_BUG_URL.format(bug=bug_id), f"LP#{bug_id}", fmt)


def _status_box(exc: MigrationExcuse) -> str:
    """Return a coloured \u25a0 indicating migration status."""
    if exc.is_candidate:
        color = _COLOR_GREEN
    elif exc.bugs:
        color = _COLOR_YELLOW
    else:
        color = _COLOR_RED
    return f"{color}{_BOX}{_COLOR_RESET}"


def _pad(hyperlinked: str, raw_text: str, width: int) -> str:
    """Return hyperlinked text followed by enough spaces to fill *width* visual columns."""
    return hyperlinked + " " * max(0, width - len(raw_text))


def _notes(exc: MigrationExcuse, fmt: OutputFormat) -> str:
    parts: list[str] = []
    if exc.bugs:
        parts.append("  ".join(_bug_link(b, fmt) for b in exc.bugs))
    if exc.reasons:
        raw = f"[{', '.join(exc.reasons)}]"
        url = _EXCUSES_URL.format(pkg=exc.package)
        parts.append(hyperlink(url, raw, fmt))
    return "  ".join(parts)


def _print_terminal_table(excuses: list[MigrationExcuse], cfg: OutputConfig) -> None:
    fmt = cfg.fmt
    pkg_w = max(20, *(len(e.package) for e in excuses))
    old_new_w = max(
        15,
        *(len(e.old_version) + 4 + len(e.new_version) for e in excuses),  # +4 for " → "
    )

    header = "  %-*s | %-*s | %-10s | %s" % (
        pkg_w,
        "Package",
        old_new_w,
        "Old \u2192 New",
        "Since",
        "Notes",
    )
    print(header, file=cfg.out)

    for exc in excuses:
        since_str = exc.in_proposed_since.strftime("%Y-%m-%d")

        raw_pkg = truncate_string(exc.package, pkg_w)
        pkg_cell = _pad(_pkg_link(exc.package, fmt), raw_pkg, pkg_w)

        raw_old_new = f"{exc.old_version} \u2192 {exc.new_version}"
        old_new_cell = _pad(
            f"{_version_link(exc.package, exc.old_version, fmt)} \u2192 "
            f"{_version_link(exc.package, exc.new_version, fmt)}",
            raw_old_new,
            old_new_w,
        )

        box = _status_box(exc)
        notes = _notes(exc, fmt)
        print(f"{box} {pkg_cell} | {old_new_cell} | {since_str} | {notes}", file=cfg.out)


@dataclass
class ProposedMigrationTriage(TriageResult):
    data: ProposedMigrationData
    teams: list[str]

    @property
    def had_updates(self) -> bool:
        return bool(self.data.excuses)

    async def print_section(self, cfg: OutputConfig) -> None:
        excuses = self.data.excuses
        count = len(excuses)
        plural = "package" if count == 1 else "packages"

        match cfg.fmt:
            case OutputFormat.TERMINAL:
                gen = ""
                if self.data.generated_date:
                    gen = f"  [generated {self.data.generated_date.strftime('%Y-%m-%d %H:%M')} UTC]"
                print(f"## Proposed Migration ({count} {plural}){gen}", file=cfg.out)
                teams_str = "|".join(self.teams)
                print(f"filter: teams={teams_str}", file=cfg.out)
                if not excuses:
                    print("  (none)", file=cfg.out)
                else:
                    _print_terminal_table(excuses, cfg)

            case OutputFormat.MARKDOWN:
                print("## Proposed Migration", file=cfg.out)
                # Only show packages that are not candidates (red/orange) — the
                # triager needs to investigate these; green ones will self-resolve.
                blocked = [e for e in excuses if not e.is_candidate]
                if not blocked:
                    print("*(none blocked)*", file=cfg.out)
                else:
                    for exc in blocked:
                        pkg_url = _EXCUSES_URL.format(pkg=exc.package)
                        pkg = hyperlink(pkg_url, exc.package, cfg.fmt)
                        new_v = _version_link(exc.package, exc.new_version, cfg.fmt)
                        line = f"#### {pkg} {new_v}"
                        if exc.bugs:
                            bug_str = " ".join(_bug_link(b, cfg.fmt) for b in exc.bugs)
                            line += f" ({bug_str})"
                        print(line, file=cfg.out)
                        print("", file=cfg.out)  # blank line for triager notes

            case _:
                raise NotImplementedError

    async def record(self, persistor: BugPersistor) -> None:
        pass  # proposed migration items are not LP bugs


async def find(
    config: StarTriageConfig,
    opts: TaskFilterOptions,
    mode: FetchMode,
) -> TriageResult:
    if mode != FetchMode.triage:
        return ProposedMigrationTriage(
            data=ProposedMigrationData(generated_date=None, excuses=[]),
            teams=[],
        )

    team_config = config.get_team(opts.team)
    teams = team_config.proposed_migration_teams
    min_age = config.general.proposed_min_age

    if not teams:
        return ProposedMigrationTriage(
            data=ProposedMigrationData(generated_date=None, excuses=[]),
            teams=[],
        )

    async with aiohttp.ClientSession() as session:
        data = await fetch_proposed_migration(teams=teams, min_age=min_age, session=session)

    return ProposedMigrationTriage(data=data, teams=teams)
