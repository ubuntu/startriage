"""Loop detection for agent tool calls (ported from git-ubuntu's copilot supervisor).

Weak models tend to repeat the identical tool call, or crawl a text a few lines
at a time with calls that differ only in numbers. From the ``deny_after``-th
repetition on, the call is denied and the model is told it is looping; from the
``abort_after``-th on, the session is aborted. Calls differing only in numbers
get ``similar_factor`` times the allowance. There is no operator to ask: runs
are unattended.

A session also gets at most ``max_calls`` tool calls (``exempt`` tools, the
submit tools, do not count and are never denied for it): past that, every
other call is denied and the model is told to submit what it has.
"""

from __future__ import annotations

import collections
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

ABORTED = "The task was aborted."
MAX_CALLS = 40
OVER_BUDGET = "Tool budget reached: no more lookups. Submit your result now with what you have."


class Supervisor:
    def __init__(
        self,
        deny_after: int = 3,
        abort_after: int = 5,
        similar_factor: int = 2,
        *,
        max_calls: int = MAX_CALLS,
        exempt: frozenset[str] = frozenset(),
    ) -> None:
        self.aborted = False
        self._deny_after = deny_after
        self._abort_after = abort_after
        self._similar_factor = similar_factor
        self._max_calls = max_calls
        self._exempt = exempt
        self._budgeted = 0
        self._calls: collections.Counter[tuple[str, str]] = collections.Counter()
        self._similar_calls: collections.Counter[tuple[str, str]] = collections.Counter()

    def check(self, tool_name: str, args: Any) -> str | None:
        """Return why the call is denied, or ``None`` to let it run."""
        if self.aborted:
            return ABORTED
        if tool_name not in self._exempt:
            self._budgeted += 1
            if self._budgeted > self._max_calls:
                logger.info(
                    "AI tool call denied (%s): budget of %d calls reached", tool_name, self._max_calls
                )
                return OVER_BUDGET
        key = (tool_name, json.dumps(args, sort_keys=True, default=str))
        similar_key = (tool_name, re.sub(r"\d+", "#", key[1]))
        self._calls[key] += 1
        self._similar_calls[similar_key] += 1
        times = self._calls[key]
        similar_times = self._similar_calls[similar_key]
        if times >= self._deny_after:
            looping = f"Are you looping? You ran the identical call {times} times."
            level = times
        elif similar_times >= self._deny_after * self._similar_factor:
            looping = (
                f"Are you looping? You made {similar_times} calls differing only in numbers; "
                "stop crawling and take a different approach."
            )
            level = similar_times // self._similar_factor
        else:
            return None
        if level >= self._abort_after:
            logger.error("AI keeps repeating a tool call, aborting: %s", looping)
            self.aborted = True
            return ABORTED
        logger.info("AI tool call denied (%s): %s", tool_name, looping)
        return looping
