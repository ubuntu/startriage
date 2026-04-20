"""Bug progress persistence: unified save/compare/postpone across LP and GitHub."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

import yaml

# --------------------------------------------------------------------------- #
# File format                                                                  #
# --------------------------------------------------------------------------- #
# v1 (legacy): a plain YAML list of bug-number strings → treated as launchpad #
# v2: a YAML dict {"version": 2, "launchpad": [...], "github": [...]}         #
# --------------------------------------------------------------------------- #
_FORMAT_VERSION = 2


def _load_yaml(path: Path | None) -> object:
    """Load a YAML file, returning None if the file does not exist."""
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or None
    except FileNotFoundError:
        return None


def _parse_compare(data: object) -> dict[str, list[str]]:
    """Return per-source ID lists from a compare-file payload.

    Backward-compatible: a plain list is treated as launchpad bug numbers.
    """
    if data is None:
        return {}
    if isinstance(data, list):
        return {"launchpad": [str(x) for x in data]}
    if isinstance(data, dict):
        return {
            str(k): [str(x) for x in v] for k, v in data.items() if k != "version" and isinstance(v, list)
        }
    return {}


class BugPersistor:
    """Manages loading, accumulating, and flushing bug state for one run.

    Usage::

        handler = get_bug_persistor(general_config.savebugs_dir, ...)
        former_lp = handler.former_bugs("launchpad")
        former_gh = handler.former_bugs("github")
        # ... render bugs, pass handler to print functions ...
        handler.record("launchpad", reported_lp_ids)
        handler.record("github", reported_gh_ids)
        handler.flush()
    """

    def __init__(
        self,
        save_path: Path | None,
        compare_path: Path | None,
        postponed_path: Path | None,
        *,
        no_save: bool = False,
    ) -> None:
        self.save_path = save_path
        self.compare_path = compare_path
        self.postponed_path = postponed_path
        self.no_save = no_save

        self._compare: dict[str, list[str]] = _parse_compare(_load_yaml(compare_path))
        self._pending: dict[str, list[str]] = {}  # source → recorded IDs

    # ---------------------------------------------------------------------- #
    # Read interface                                                           #
    # ---------------------------------------------------------------------- #

    def former_bugs(self, source: str) -> list[str]:
        """Return the list of IDs from the compare file for *source*."""
        return self._compare.get(source, [])

    def load_postponed(self, out: IO[str] = sys.stdout) -> list[str]:
        """Print the postponed-bugs table and return the list of still-active IDs."""
        postponed: list[str] = []
        print("\nPostponed bugs:", file=out)
        data = _load_yaml(self.postponed_path)
        if data and isinstance(data, list):
            for entry in data:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                until = datetime.strptime(str(entry[1]), "%Y-%m-%d")
                if until.date() > datetime.now().date():
                    print(f"{entry[0]} postponed until {entry[1]}", file=out)
                    postponed.append(str(entry[0]))
        if not postponed:
            print("<None>", file=out)
        print("", file=out)
        return postponed

    # ---------------------------------------------------------------------- #
    # Write interface                                                          #
    # ---------------------------------------------------------------------- #

    def record(self, source: str, ids: list[str]) -> None:
        """Accumulate *ids* for *source* (may be called multiple times)."""
        self._pending.setdefault(source, []).extend(ids)

    def flush(self) -> None:
        """Write the combined save file (no-op when saving is disabled)."""
        if self.no_save or not self.save_path:
            return
        payload: dict = {"version": _FORMAT_VERSION}
        # Preserve any sources from the compare file that were not re-recorded
        for source, ids in self._compare.items():
            if source not in self._pending:
                payload[source] = ids
        payload.update(self._pending)
        with self.save_path.open("w", encoding="utf-8") as fh:
            yaml.dump(payload, stream=fh)
        logging.info("Saved bug state to %s", self.save_path)


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #


def get_bug_persistor(
    savebugs_dir: Path | None,
    *,
    filename_save: Path | None = None,
    filename_compare: Path | None = None,
    filename_postponed: Path | None = None,
    no_save: bool = False,
    prefix: str = "todo",
) -> BugPersistor:
    """Build a :class:`BugPersistor` from directory + optional path overrides.

    When *savebugs_dir* is ``None`` and no explicit paths are supplied the
    handler is effectively a no-op (no saving, no comparing).
    """
    if not no_save and savebugs_dir is not None:
        savebugs_dir.mkdir(parents=True, exist_ok=True)
        auto_save = savebugs_dir / f"{prefix}-{datetime.now().strftime('%Y-%m-%d')}.yaml"
        save_path = filename_save or auto_save

        if filename_compare is None:
            existing = sorted(savebugs_dir.glob(f"{prefix}-*.yaml"))
            compare_path = existing[-1] if existing else None
        else:
            compare_path = filename_compare

        auto_postponed = savebugs_dir / "postponed.yaml"
        postponed_path = filename_postponed or (auto_postponed if auto_postponed.exists() else None)

    elif filename_save or filename_compare or filename_postponed:
        save_path = filename_save
        compare_path = filename_compare
        postponed_path = filename_postponed

    else:
        save_path = compare_path = postponed_path = None

    if save_path and not no_save:
        logging.info("Will save bug state to: %s", save_path)

    return BugPersistor(save_path, compare_path, postponed_path, no_save=no_save)
