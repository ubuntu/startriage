"""Guard the snap/pyproject plumbing that ships the AI triage feature.

These are offline structural checks — they do not build the snap — so a future
edit cannot silently drop the Copilot runtime, ubuntu-dev-tools, the writable
COPILOT_HOME, or the required plugs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SNAPCRAFT = _ROOT / "snapcraft.yaml"
_PYPROJECT = _ROOT / "pyproject.toml"


def _snapcraft() -> dict:
    return yaml.safe_load(_SNAPCRAFT.read_text())


def test_app_keeps_network_and_home_plugs():
    plugs = _snapcraft()["apps"]["startriage"]["plugs"]
    assert {"network", "network-bind", "home"} <= set(plugs)


def test_copilot_home_points_at_writable_dir():
    env = _snapcraft()["apps"]["startriage"]["environment"]
    # ~/.copilot is hidden and blocked by the home plug; must be under SNAP_USER_DATA.
    assert "SNAP_USER_DATA" in env["COPILOT_HOME"]


def test_part_ships_copilot_sdk_and_ubuntu_dev_tools():
    part = _snapcraft()["parts"]["startriage"]
    assert "github-copilot-sdk" in part["python-packages"]
    assert "ubuntu-dev-tools" in part["stage-packages"]


def test_pyproject_exposes_optional_ai_extra():
    data = tomllib.loads(_PYPROJECT.read_text())
    assert data["project"]["optional-dependencies"]["ai"] == ["github-copilot-sdk"]
