"""Data models for proposed migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MigrationExcuse:
    package: str
    old_version: str  # '-' for new packages
    new_version: str
    in_proposed_since: datetime  # when the package entered -proposed
    is_candidate: bool = False  # True = passing all checks, just waiting to migrate
    reasons: list[str] = field(default_factory=list)
    bugs: list[int] = field(default_factory=list)  # LP bug IDs from update-excuse


@dataclass
class ProposedMigrationData:
    generated_date: datetime | None
    excuses: list[MigrationExcuse]
