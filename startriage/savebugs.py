"""Bug progress persistence: unified save/compare/postpone across LP and GitHub."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

# File format:
# v1 (legacy): a plain YAML list of bug-number strings → treated as launchpad
# v2: a YAML dict {"version": 2, "launchpad": [...], "github": [...]}
_FORMAT_VERSION = 2


@dataclass
class SaveConfig:
    savebugs_dir: Path | None
    override_save: Path | None
    override_compare: Path | None
    no_save: bool


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
        handler.save()
    """

    def __init__(
        self,
        cfg: SaveConfig,
    ) -> None:
        self._cfg = cfg

        today = datetime.now().strftime("%Y-%m-%d")

        self._save_path = cfg.override_save
        if cfg.savebugs_dir and not cfg.override_save:
            save_default = cfg.savebugs_dir / f"todo-{today}.yaml"
            self._save_path = save_default

        compare_path = cfg.override_compare
        if cfg.savebugs_dir and not cfg.override_compare:
            existing = cfg.savebugs_dir.glob("todo-*.yaml")

            # take the latest (by name)
            for p in sorted(existing, reverse=True):
                if p.name == f"todo-{today}.yaml":
                    continue
                compare_path = p
                break
            else:
                compare_path = None

        if self._save_path and not cfg.no_save:
            logging.info("Will save bug state to: %s", self._save_path)

        if compare_path:
            with compare_path.open(encoding="utf-8") as fh:
                previous_items = yaml.safe_load(fh)
        else:
            previous_items = None

        self._previous_items: dict[str, list[str]] = _parse_compare(previous_items)
        self._pending: dict[str, list[str]] = {}  # source → recorded IDs

    def former_bugs(self, source: str) -> list[str]:
        """Return the list of IDs from the compare file for *source*."""
        return self._previous_items.get(source, [])

    def record(self, source: str, ids: list[str]) -> None:
        """Accumulate *ids* for *source* (may be called multiple times)."""
        self._pending.setdefault(source, []).extend(ids)

    def save(self) -> None:
        """Write the combined save file (no-op when saving is disabled)."""
        if self._cfg.no_save or not self._save_path:
            return
        payload: dict = {"version": _FORMAT_VERSION}

        # Preserve any sources from the compare file that were not re-recorded
        for source, ids in self._previous_items.items():
            if source not in self._pending:
                payload[source] = ids
        payload.update(self._pending)

        with self._save_path.open("w", encoding="utf-8") as fh:
            yaml.dump(payload, stream=fh)

        logging.info("Saved bug state to %s", self._save_path)

    @property
    def compare_path(self) -> Path | None:
        """Return the path used for comparison (if any)."""
        return self._cfg.override_compare or self._save_path

    @property
    def no_save(self) -> bool:
        """Return True if saving is disabled."""
        return self._cfg.no_save
