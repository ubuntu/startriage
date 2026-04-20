"""Shared StrEnum types used across startriage modules."""

from __future__ import annotations

from enum import StrEnum


class UpdateFilter(StrEnum):
    """Controls which bugs are shown based on who last acted on them."""

    theirs = "theirs"  # show only bugs where non-team member acted last
    ours = "ours"  # show only bugs where team member acted last
    all = "all"  # show all bugs regardless of last actor


class FetchMode(StrEnum):
    """Launchpad bug fetch mode."""

    triage = "triage"  # date-range bugs for daily triage
    todo = "todo"  # tag-filtered housekeeping bugs
    subscribed = "subscribed"  # directly subscribed bugs
