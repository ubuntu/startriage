"""Skills: the instructions for each phase, shipped as markdown resources.

A skill is ``data/skills/<name>.md`` with a YAML front-matter (``name``,
``description``, ``tools``: the host tools the phase may use, ``required``:
tools without which the phase is skipped, ``a|b`` meaning either, ``submit``:
its contract tool, ``max_calls``: the session's tool call budget) and a markdown
body. ``triage-core`` is prepended to every
phase. Package notes in ``data/skills/packages/pkg-<package>.md`` add a
``## <phase>`` section to that phase, and may list ``also_search`` packages.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

from .supervisor import MAX_CALLS

CORE = "triage-core"
# Model phases (one skill each), in pipeline order.
PHASES = (
    "review-update",
    "validate-report",
    "search-upstream",
    "search-other-distros",
    "assess-existing",
    "check-regression",
    "analyze-source",
    "propose-fix",
    "write-report",
)
# All phases, host ones included, in pipeline order.
PIPELINE = ("precheck", "review-update", "validate-report", "search", *PHASES[2:])
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_SECTION = re.compile(r"^## +(\S+)[ \t]*$", re.M)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple[str, ...]
    submit: str
    body: str
    sha256: str
    required: tuple[str, ...] = ()
    max_calls: int = MAX_CALLS

    def missing(self, enabled: Collection[str]) -> list[str]:
        """The required tools (or alternatives, ``a|b``) not in ``enabled``."""
        return [need for need in self.required if not any(t in enabled for t in need.split("|"))]


@dataclass(frozen=True)
class PackageNotes:
    package: str
    also_search: tuple[str, ...]
    sections: dict[str, str]


def _split(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("skill front-matter must be a mapping")
    return meta, text[match.end() :]


def _skills_dir() -> Any:
    return files("startriage") / "data" / "skills"


@cache
def load_skill(name: str) -> Skill:
    text = (_skills_dir() / f"{name}.md").read_text(encoding="utf-8")
    meta, body = _split(text)
    return Skill(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        tools=tuple(meta.get("tools") or ()),
        submit=meta.get("submit", ""),
        body=body.strip(),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        required=tuple(meta.get("required") or ()),
        max_calls=int(meta.get("max_calls") or MAX_CALLS),
    )


@cache
def load_package_notes(package: str) -> PackageNotes | None:
    path = _skills_dir() / "packages" / f"pkg-{package}.md"
    if not path.is_file():
        return None
    meta, body = _split(path.read_text(encoding="utf-8"))
    parts = _SECTION.split(body)
    # parts = [preamble, name1, text1, name2, text2, ...]
    sections = {name: text.strip() for name, text in zip(parts[1::2], parts[2::2], strict=True)}
    return PackageNotes(package=package, also_search=tuple(meta.get("also_search") or ()), sections=sections)


def system_prompt(phase: str, packages: list[str]) -> str:
    """The system prompt of ``phase``: the core rules, the phase skill and package notes."""
    parts = [load_skill(CORE).body, load_skill(phase).body]
    for package in dict.fromkeys(packages):
        notes = load_package_notes(package) if package else None
        if notes and phase in notes.sections:
            parts.append(f"## Notes for {package}\n\n{notes.sections[phase]}")
    return "\n\n".join(parts)


def also_search(packages: list[str]) -> list[str]:
    extra: list[str] = []
    for package in packages:
        notes = load_package_notes(package) if package else None
        if notes:
            extra.extend(notes.also_search)
    return extra
