"""Tests for startriage.config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from startriage.config import load_config


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
