"""Skill feedback: what the model says is unclear or missing in a skill.

Enabled with ``[ai] suggest_improvements = true``. Every ``submit_*`` tool
then takes an optional ``skill_feedback``; non-empty feedback is appended to
a JSON-lines log (one object per line) so skills can be improved from real
runs, and grouped per skill in the report.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import platformdirs


def default_feedback_log() -> Path:
    return platformdirs.user_state_path("startriage") / "skill-feedback.jsonl"


def _version() -> str:
    try:
        return version("startriage")
    except PackageNotFoundError:
        return "unknown"


def record_feedback(entries: list[dict[str, Any]], path: Path) -> None:
    """Append ``entries`` (skill, skill_sha256, bug, model, feedback) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = {"time": datetime.now(UTC).isoformat(timespec="seconds"), "version": _version()}
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(stamp | entry, ensure_ascii=False) + "\n")
