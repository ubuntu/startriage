"""Configuration loading and validation for startriage."""

from __future__ import annotations

import os
import tomllib
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .enums import AIProvider, UpdateFilter


def default_config_path() -> Path:
    """Default config path, respecting snap confinement."""
    if snap_data := os.environ.get("SNAP_USER_DATA"):
        return Path(snap_data) / ".config" / "startriage.toml"
    return Path("~/.config/startriage.toml")


DEFAULT_USER_CONFIG = default_config_path()

# Environment variables consulted for AI credentials, in priority order.
# Copilot mirrors the GitHub Copilot SDK's own precedence.
COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
OPENROUTER_KEY_ENV_VARS = ("STARTRIAGE_AI_OPENROUTER_KEY", "OPENROUTER_API_KEY")


def _first_env(names: tuple[str, ...]) -> str | None:
    """Return the first non-empty value among the given environment variables."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


class AIConfigError(Exception):
    """Raised when the [ai] section lacks the credentials required to run."""


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AIProvider = AIProvider.copilot
    model: str = "claude-opus-4.8"
    # Copilot auth (or rely on COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN env).
    github_token: str | None = None
    # OpenRouter (BYOK) auth.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    def resolve_token(self) -> str | None:
        """Return the effective credential for the active provider.

        Config values take precedence over environment variables.
        """
        if self.provider is AIProvider.copilot:
            return self.github_token or _first_env(COPILOT_TOKEN_ENV_VARS)
        return self.openrouter_api_key or _first_env(OPENROUTER_KEY_ENV_VARS)

    def require_configured(self) -> None:
        """Raise AIConfigError with a friendly hint when no credential is available."""
        if self.resolve_token():
            return
        if self.provider is AIProvider.copilot:
            raise AIConfigError(
                "No Copilot credential configured. Run "
                "'startriage config set --ai-github-token <token>' or set the "
                "COPILOT_GITHUB_TOKEN environment variable."
            )
        raise AIConfigError(
            "No OpenRouter API key configured. Run "
            "'startriage config set --ai-openrouter-key <key>' or set the "
            "OPENROUTER_API_KEY environment variable."
        )


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lp_expire_level1_days: int = 60
    lp_expire_level2_days: int = 180
    lp_extended: bool | None = None
    lp_triage_updates: UpdateFilter = UpdateFilter.theirs
    savebugs_dir: Path | None = None
    default_team: str | None = None
    proposed_min_age: int = 4
    github_token: str | None = None

    @model_validator(mode="after")
    def expand_savebugs_dir(self) -> GeneralConfig:
        if self.savebugs_dir is not None:
            self.savebugs_dir = self.savebugs_dir.expanduser()
            if not self.savebugs_dir.is_dir():
                raise ValueError(f"savebugs_dir {self.savebugs_dir!r} is not a directory")
        return self


class GithubRepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str  # org/reponame
    todo_labels: list[str] | None = None
    watch_labels: list[str] | None = None

    @classmethod
    def from_str_or_dict(cls, v: object) -> GithubRepoConfig:
        """Allow a plain string "org/repo" as shorthand for {name = "org/repo"}."""
        if isinstance(v, str):
            return cls(name=v)
        return cls.model_validate(v)


class TeamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lp_team: str
    lp_todo_tag: str
    lp_ignore_packages: list[str] = []
    discourse_categories: list[str] = []
    discourse_triage_categories: list[str] = []
    github_todo_labels: list[str] | None = None  # overridden by github_repos[*].todo_labels
    # TODO: github_watch_labels: list[str] | None = None  # overridden by github_repos[*].watch_labels
    github_repos: list[GithubRepoConfig] = []
    proposed_migration_teams: list[str] = []

    @field_validator("github_repos", mode="before")
    @classmethod
    def coerce_github_repos(cls, v: object) -> list[GithubRepoConfig]:
        """Accept both plain strings and dicts/GithubRepoConfig objects."""
        if not isinstance(v, list):
            raise ValueError("github_repos must be a list")
        return [GithubRepoConfig.from_str_or_dict(item) for item in v]


class StarTriageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general: GeneralConfig = GeneralConfig()
    ai: AIConfig = AIConfig()
    team: dict[str, TeamConfig] = {}
    loaded_paths: list[Path] = []

    def get_team(self, name: str) -> TeamConfig:
        """Return TeamConfig for the named team, raising KeyError if not found."""
        try:
            return self.team[name]
        except KeyError:
            available = ", ".join(sorted(self.team.keys())) or "(none)"
            raise KeyError(f"Unknown team '{name}'. Available teams: {available}") from None

    def show(self) -> str:
        data: dict = {"general": {}, "ai": {}, "team": {}}
        for field, value in self.general.model_dump(exclude_none=True).items():
            data["general"][field] = value
        for field, value in self.ai.model_dump(exclude_none=True).items():
            data["ai"][field] = value
        for team_name, team in self.team.items():
            data["team"][team_name] = team.model_dump(exclude_none=True)

        lines: list[str] = []
        for p in self.loaded_paths:
            lines.append(f"# loaded from: {p}")
        if self.loaded_paths:
            lines.append("")
        lines.append(tomli_w.dumps(data).rstrip())
        return "\n".join(lines)


def _load_toml(path: Path) -> dict:
    """Load a TOML file, returning empty dict if not found."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def update_user_config(
    updates: dict,
    config_path: Path | None = None,
    *,
    sensitive: bool = False,
) -> Path:
    """Read-modify-write the user config TOML file.

    *updates* is a nested dict merged into the existing config (e.g.
    ``{"general": {"github_token": "ghp_..."}}``).\n
    If *sensitive* is True, file permissions are set to 0o600.

    Returns the resolved path that was written.
    """
    path = (config_path or DEFAULT_USER_CONFIG).expanduser()
    data = _load_toml(path)

    # Shallow-merge each top-level section
    for section, values in updates.items():
        if isinstance(values, dict):
            data.setdefault(section, {}).update(values)
        else:
            data[section] = values

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
    if sensitive:
        path.chmod(0o600)
    return path


def _load_defaults(path: Traversable) -> dict:
    """Load the shipped defaults.toml using importlib.resources.

    Works in the git repo, as an installed package, a .deb, or a snap.
    """
    with path.open("rb") as f:
        return tomllib.load(f)


def load_config(user_config_path: Path | None) -> StarTriageConfig:
    """Load and merge defaults with user config, validated via pydantic.

    Merge strategy:
    - [general] keys are merged field-by-field (user overrides defaults)
    - [team.X] sections are merged field-by-field: user values override defaults for
      that team, missing fields fall back to the defaults entry
    - Teams only in defaults remain available; teams only in user config are added
    """
    defaults_path = files("startriage") / "data" / "defaults.toml"
    defaults = _load_defaults(defaults_path)

    path = (user_config_path or DEFAULT_USER_CONFIG).expanduser()
    user = _load_toml(path)
    loaded_paths: list[Path] = [Path(str(defaults_path))]
    if user:
        loaded_paths.append(path)

    # Merge general section
    merged_general = {**defaults.get("general", {}), **user.get("general", {})}

    # Merge ai section (user overrides defaults field-by-field)
    merged_ai = {**defaults.get("ai", {}), **user.get("ai", {})}

    # Merge team sections field-by-field so a sparse user section doesn't lose defaults
    default_teams = defaults.get("team", {})
    user_teams = user.get("team", {})
    all_team_names = set(default_teams) | set(user_teams)
    merged_teams = {
        name: {**default_teams.get(name, {}), **user_teams.get(name, {})} for name in all_team_names
    }

    return StarTriageConfig.model_validate(
        {"general": merged_general, "ai": merged_ai, "team": merged_teams, "loaded_paths": loaded_paths}
    )


def resolve_team_name(team_arg: str | None, config: StarTriageConfig) -> str:
    """Determine which team to use.

    Priority:
    1. Explicit -t/--team argument
    2. general.default_team in config
    3. If exactly one team is configured, use it automatically
    """
    if team_arg:
        return team_arg
    default = config.general.default_team
    if default:
        return default
    teams = list(config.team.keys())
    if len(teams) == 1:
        return teams[0]

    available = ", ".join(sorted(teams)) or "(none)"
    raise KeyError(f"Multiple teams configured; use -t to pick one: {available}")
