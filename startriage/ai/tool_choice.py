"""Choosing the agent's tools: ``startriage config ai-tools`` and the first run."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from itertools import groupby
from pathlib import Path

from ..config import StarTriageConfig, update_user_config
from .tools import REGISTRY, Risk, enabled_tool_names

logger = logging.getLogger(__name__)


def format_tools(enabled: set[str], numbered: bool = False) -> str:
    """List every tool by group, with its state and what it does."""
    lines: list[str] = []
    width = max(len(name) for name in REGISTRY)
    number = 0
    for group, tools in groupby(REGISTRY.values(), key=lambda tool: tool.group):
        lines.append(group)
        for tool in tools:
            number += 1
            prefix = f"{number:>3} " if numbered else "  "
            mark = "x" if tool.name in enabled else " "
            note = "  [runs programs on this machine]" if tool.risk is Risk.exec else ""
            lines.append(f"{prefix}[{mark}] {tool.name:<{width}}  {tool.summary}{note}")
    return "\n".join(lines)


def choose_tools(
    enabled: set[str],
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> set[str] | None:
    """Interactive checklist; returns the chosen tools, or ``None`` if cancelled."""
    names = list(REGISTRY)
    chosen = set(enabled)
    while True:
        say(format_tools(chosen, numbered=True))
        try:
            answer = ask("Toggle tools by number, Enter to save, q to cancel: ").strip().lower()
        except EOFError:
            return None
        if answer == "":
            return chosen
        if answer == "q":
            return None
        for token in answer.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(names):
                chosen ^= {names[int(token) - 1]}
            else:
                say(f"Not a tool number: {token}")


def save_tools(enabled: set[str], config_path: Path | None) -> Path:
    """Persist the choice for every known tool to ``[ai.tools]``."""
    choice = {name: name in enabled for name in REGISTRY}
    return update_user_config({"ai": {"tools": choice}}, config_path=config_path)


def resolve_tools(config: StarTriageConfig, config_path: Path | None) -> None:
    """On the first ``--ai`` run, let the user choose the tools (on a TTY).

    Without a TTY the defaults (read tools) are used and nothing is saved.
    """
    if config.ai.tools is not None:
        return
    defaults = set(enabled_tool_names(config.ai))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        logger.warning(
            "AI agent tools not chosen yet, using the defaults: %s. Run 'startriage config ai-tools'.",
            ", ".join(sorted(defaults)),
        )
        return
    print("Choose the tools the AI agent may use (change later with 'startriage config ai-tools').")
    chosen = choose_tools(defaults)
    if chosen is None:
        chosen = defaults
        print("Not saved; using the defaults for this run.")
    else:
        print(f"Saved to {save_tools(chosen, config_path)}")
    config.ai = config.ai.model_copy(update={"tools": {name: name in chosen for name in REGISTRY}})
