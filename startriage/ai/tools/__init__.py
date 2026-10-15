"""Typed host tools the agent can call, and the user's choice of them.

Every tool is listed in :data:`REGISTRY`. The user enables tools once per
machine in ``[ai.tools]``; a tool missing from that table gets its default,
so new read tools are enabled automatically and new exec tools stay off.
"""

from __future__ import annotations

import logging

from ...config import AIConfig
from . import debian, forges, launchpad, local, patch, source, web
from .base import Risk, ToolContext, ToolDef, ToolError, ToolSpec, call_tool
from .http import ReadOnlyHTTP
from .submit import Submission

logger = logging.getLogger(__name__)

# Grouped: the menu lists the tools group by group, in this order.
REGISTRY: dict[str, ToolDef] = {
    tool.name: tool
    for tool in [
        *launchpad.TOOLS,
        *source.TOOLS,
        *patch.TOOLS,
        *local.TOOLS,
        *debian.TOOLS,
        *forges.TOOLS,
        *web.TOOLS,
    ]
}


def enabled_tool_names(ai_config: AIConfig) -> list[str]:
    """Return the enabled tools: the user's choice, or each tool's default."""
    chosen = ai_config.tools or {}
    unknown = sorted(set(chosen) - REGISTRY.keys())
    if unknown:
        logger.warning("Ignoring unknown tools in [ai.tools]: %s", ", ".join(unknown))
    names = [name for name, tool in REGISTRY.items() if chosen.get(name, tool.default)]
    if "fetch_source" not in names:
        names = [name for name in names if name not in local.NEEDS_FETCH]
    return names


def build_tools(ctx: ToolContext, names: list[str]) -> list[ToolSpec]:
    """Bind the named tools to ``ctx``."""
    return [REGISTRY[name].bind(ctx) for name in names]


__all__ = [
    "REGISTRY",
    "ReadOnlyHTTP",
    "Risk",
    "Submission",
    "ToolContext",
    "ToolDef",
    "ToolError",
    "ToolSpec",
    "build_tools",
    "call_tool",
    "enabled_tool_names",
]
