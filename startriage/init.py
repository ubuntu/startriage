"""Interactive creation of the user config file (``startriage init``)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import tomli_w

from .config import AIProvider, StarTriageConfig, load_config

Ask = Callable[[str], str]


def _ask(ask: Ask, question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = ask(f"{question}{suffix}: ").strip()
    return answer or (default or "")


def _ask_yes(ask: Ask, question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{question} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _ask_list(ask: Ask, question: str, default: list[str] | None = None) -> list[str]:
    answer = _ask(ask, f"{question} (comma separated)", ",".join(default or []))
    return [item.strip() for item in answer.split(",") if item.strip()]


def _ask_choice(ask: Ask, question: str, choices: list[str], default: str) -> str:
    while True:
        answer = _ask(ask, f"{question} ({'/'.join(choices)})", default)
        if answer in choices:
            return answer
        print(f"Please choose one of: {', '.join(choices)}")


def _ask_team(ask: Ask, name: str, base: dict) -> dict:
    """Ask for the settings of one team, offering values from *base* as defaults."""
    team: dict = {}
    team["lp_team"] = _ask(ask, "Launchpad team whose bug subscriptions to triage", base.get("lp_team", name))
    team["lp_todo_tag"] = _ask(
        ask, "Launchpad tag marking actionable bugs", base.get("lp_todo_tag", f"{name}-todo")
    )
    if freezer := _ask(ask, "Launchpad tag for postponed bugs (empty for none)", base.get("lp_freezer_tag")):
        team["lp_freezer_tag"] = freezer
    if repos := _ask_list(
        ask,
        "GitHub repositories to watch, as org/repo (e.g. canonical/ubuntu-maintainers-handbook, "
        "canonical/pollen)",
        [r if isinstance(r, str) else r["name"] for r in base.get("github_repos", [])],
    ):
        team["github_repos"] = repos
        if labels := _ask_list(ask, "GitHub labels marking actionable items", base.get("github_todo_labels")):
            team["github_todo_labels"] = labels
        if gh_team := _ask(
            ask,
            "GitHub team whose activity counts as yours, as org/team "
            "(e.g. canonical/ubuntu-server-distro, empty for none)",
            base.get("github_team"),
        ):
            team["github_team"] = gh_team
    if categories := _ask_list(ask, "Discourse categories to watch", base.get("discourse_categories")):
        team["discourse_categories"] = categories
    if migration := _ask_list(
        ask, "Teams for proposed-migration tracking", base.get("proposed_migration_teams")
    ):
        team["proposed_migration_teams"] = migration
    return team


def build_init_config(defaults: StarTriageConfig, ask: Ask = input) -> dict:
    """Ask the user questions and return the config data to write."""
    data: dict = {"general": {}, "team": {}}

    print("Teams shipped with startriage:", ", ".join(sorted(defaults.team)) or "(none)")
    name = _ask(
        ask, "Team to triage (pick one above or name a new one)", next(iter(sorted(defaults.team)), None)
    )
    while not name:
        name = _ask(ask, "Team name")

    known = name in defaults.team
    if not known or _ask_yes(ask, f"Customize the settings of team '{name}'?", default=False):
        base = defaults.team[name].model_dump(mode="json", exclude_none=True) if known else {}
        team = _ask_team(ask, name, base)
        if known:
            # only store what differs from the shipped defaults
            team = {k: v for k, v in team.items() if base.get(k) != v}
        if team or not known:
            data["team"][name] = team
    data["general"]["default_team"] = name

    savebugs = _ask(ask, "Directory to save 'todo' runs in, for comparing them (empty to skip)")
    if savebugs:
        path = Path(savebugs).expanduser()
        if not path.is_dir() and _ask_yes(ask, f"Create directory {path}?", default=True):
            path.mkdir(parents=True)
        data["general"]["savebugs_dir"] = savebugs

    if _ask_yes(ask, "Set up AI-assisted triage (--ai)?", default=False):
        provider = _ask_choice(ask, "AI provider", list(AIProvider), str(AIProvider.copilot))
        model = ""
        while not model:
            model = _ask(ask, "Model id, as named by the provider")
        data["ai"] = {"provider": provider, "model": model}
        print("Set the credential with 'startriage config set' or an environment variable (see --help).")

    if not data["team"]:
        del data["team"]
    return data


def write_init_config(path: Path, data: dict) -> None:
    """Write *data* as the new user config to *path* and check that it loads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    load_config(path)
