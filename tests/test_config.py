"""Tests for startriage.config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from startriage.config import AIConfig, AIConfigError, load_config, update_user_config
from startriage.enums import AIProvider


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "startriage.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_load_defaults_only(tmp_path):
    """load_config with no user file still returns built-in ubuntu-server team."""
    config = load_config(tmp_path / "nonexistent.toml")
    assert "ubuntu-server" in config.team
    team = config.team["ubuntu-server"]
    assert team.lp_team == "ubuntu-server"
    assert team.lp_todo_tag == "server-todo"
    assert "cloud-init" in team.lp_ignore_packages


def test_general_override(tmp_path):
    bugs_dir = tmp_path / "savebugs"
    bugs_dir.mkdir()
    p = _write_toml(
        tmp_path,
        f"""\
        [general]
        lp_extended = true
        savebugs_dir = "{bugs_dir}"
        """,
    )
    config = load_config(p)
    assert config.general.lp_extended is True
    assert config.general.savebugs_dir == bugs_dir


def test_team_override_replaces_ignore_list(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [team.ubuntu-server]
        lp_team = "ubuntu-server"
        lp_todo_tag = "server-todo"
        lp_ignore_packages = []
        discourse_categories = ["project/server"]
        github_repos = []
        """,
    )
    config = load_config(p)
    assert config.team["ubuntu-server"].lp_ignore_packages == []


def test_custom_team_added(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [team.ubuntu-desktop]
        lp_team = "ubuntu-desktop"
        lp_todo_tag = "desktop-todo"
        lp_ignore_packages = []
        discourse_categories = ["desktop"]
        github_repos = []
        """,
    )
    config = load_config(p)
    assert "ubuntu-desktop" in config.team
    assert "ubuntu-server" in config.team  # defaults still present


def test_get_team_known(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    team = config.get_team("ubuntu-server")
    assert team.lp_team == "ubuntu-server"


def test_get_team_unknown(tmp_path):
    config = load_config(tmp_path / "nonexistent.toml")
    with pytest.raises(KeyError, match="ubuntu-bogus"):
        config.get_team("ubuntu-bogus")


def test_invalid_lp_triage_updates_filter(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [general]
        lp_triage_updates = "invalid_value"
        """,
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_extra_field_rejected(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [general]
        typo_field = true
        """,
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_github_token_config(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [general]
        github_token = "ghp_secret"
        """,
    )
    config = load_config(p)
    assert config.general.github_token == "ghp_secret"


def test_ai_defaults(tmp_path):
    """No [ai] section yields sensible Copilot defaults."""
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.ai.provider is AIProvider.copilot
    assert config.ai.model == "claude-opus-4.8"
    assert config.ai.openrouter_base_url == "https://openrouter.ai/api/v1"


def test_ai_override(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [ai]
        provider = "openrouter"
        model = "anthropic/claude-3.5-sonnet"
        openrouter_api_key = "or_secret"
        """,
    )
    config = load_config(p)
    assert config.ai.provider is AIProvider.openrouter
    assert config.ai.model == "anthropic/claude-3.5-sonnet"
    assert config.ai.openrouter_api_key == "or_secret"


def test_ai_invalid_provider(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [ai]
        provider = "bogus"
        """,
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_ai_extra_field_rejected(tmp_path):
    p = _write_toml(
        tmp_path,
        """\
        [ai]
        typo_field = true
        """,
    )
    with pytest.raises(ValidationError):
        load_config(p)


def test_ai_resolve_token_prefers_config(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "env_token")
    p = _write_toml(
        tmp_path,
        """\
        [ai]
        github_token = "cfg_token"
        """,
    )
    config = load_config(p)
    assert config.ai.resolve_token() == "cfg_token"


def test_ai_resolve_token_from_env(tmp_path, monkeypatch):
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GH_TOKEN", "env_token")
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.ai.resolve_token() == "env_token"


def test_ai_check_token_copilot_missing(tmp_path, monkeypatch):
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    config = load_config(tmp_path / "nonexistent.toml")
    with pytest.raises(AIConfigError, match="Copilot"):
        AIConfig.model_validate(config.ai.model_dump(), context={"require_ai": True})


def test_ai_check_token_openrouter_missing(tmp_path, monkeypatch):
    for var in ("STARTRIAGE_AI_OPENROUTER_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    p = _write_toml(
        tmp_path,
        """\
        [ai]
        provider = "openrouter"
        """,
    )
    config = load_config(p)
    with pytest.raises(AIConfigError, match="OpenRouter"):
        AIConfig.model_validate(config.ai.model_dump(), context={"require_ai": True})


def test_ai_check_token_skipped_without_context(tmp_path, monkeypatch):
    # Non-AI commands validate AIConfig on every load; without the require_ai
    # context the missing-credential check must not fire.
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.ai.resolve_token() is None


def test_ai_secret_written_with_restricted_perms(tmp_path):
    path = tmp_path / "startriage.toml"
    update_user_config(
        {"ai": {"openrouter_api_key": "or_secret"}},
        config_path=path,
        sensitive=True,
    )
    assert load_config(path).ai.openrouter_api_key == "or_secret"
    assert (path.stat().st_mode & 0o777) == 0o600
