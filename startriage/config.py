"""Configuration loading and validation for startriage."""

from __future__ import annotations

import tomllib
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from startriage.enums import UpdateFilter

DEFAULT_USER_CONFIG = Path("~/.config/startriage.toml")


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discourse_site: str = "https://discourse.ubuntu.com"
    lp_expire_tagged: int = 60
    lp_expire: int = 180
    lp_extended: bool = False
    lp_update_filter: UpdateFilter = UpdateFilter.theirs
    savebugs_dir: Path = Path("~/savebugs")
    default_team: str | None = None

    @model_validator(mode="after")
    def expand_savebugs_dir(self) -> GeneralConfig:
        self.savebugs_dir = self.savebugs_dir.expanduser()
        return self


class TeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lp_team: str
    lp_todo_tag: str
    lp_ignore_packages: list[str] = []
    discourse_categories: str
    github_org: str
    github_repos: list[str]


class StarTriageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralConfig = GeneralConfig()
    team: dict[str, TeamConfig] = {}

    def get_team(self, name: str) -> TeamConfig:
        """Return TeamConfig for the named team, raising KeyError if not found."""
        try:
            return self.team[name]
        except KeyError:
            available = ", ".join(sorted(self.team.keys())) or "(none)"
            raise KeyError(f"Unknown team '{name}'. Available teams: {available}") from None


def _load_toml(path: Path) -> dict:
    """Load a TOML file, returning empty dict if not found."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def _load_defaults() -> dict:
    """Load the shipped defaults.toml using importlib.resources.

    Works in the git repo, as an installed package, a .deb, or a snap.
    """
    data_pkg = files("startriage") / "data" / "defaults.toml"
    with data_pkg.open("rb") as f:
        return tomllib.load(f)


def load_config(user_config_path: Path | None = None) -> StarTriageConfig:
    """Load and merge defaults with user config, validated via pydantic.

    Merge strategy:
    - [general] keys are merged field-by-field (user overrides defaults)
    - [team.X] sections are merged field-by-field: user values override defaults for
      that team, missing fields fall back to the defaults entry
    - Teams only in defaults remain available; teams only in user config are added
    """
    defaults = _load_defaults()

    path = (user_config_path or DEFAULT_USER_CONFIG).expanduser()
    user = _load_toml(path)

    # Merge general section
    merged_general = {**defaults.get("general", {}), **user.get("general", {})}

    # Merge team sections field-by-field so a sparse user section doesn't lose defaults
    default_teams = defaults.get("team", {})
    user_teams = user.get("team", {})
    all_team_names = set(default_teams) | set(user_teams)
    merged_teams = {
        name: {**default_teams.get(name, {}), **user_teams.get(name, {})} for name in all_team_names
    }

    return StarTriageConfig.model_validate({"general": merged_general, "team": merged_teams})
