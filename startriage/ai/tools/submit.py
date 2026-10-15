"""``submit_<phase>``: the contract tool that ends a phase session.

The model passes its result as tool arguments. They are validated against the
phase's hand-off model, then against the host checks; any error is returned
to the model so it can repair the call in the same session.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from .base import ToolError, ToolSpec

logger = logging.getLogger(__name__)

FEEDBACK_FIELD = "skill_feedback"
FEEDBACK_SCHEMA = {
    "type": "string",
    "description": (
        "Optional: if the instructions for this phase were unclear, wrong or missing something "
        "you needed, say what to change (one or two sentences). Leave empty otherwise."
    ),
}

Check = Callable[[Any], Awaitable[list[str]]]


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace pydantic's ``$ref``/``$defs`` with the definitions, for picky providers."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def submit_schema(model: type[BaseModel], *, feedback: bool = False) -> dict[str, Any]:
    """JSON schema of ``model`` as tool parameters, optionally with ``skill_feedback``."""
    schema = _inline_refs(model.model_json_schema())
    schema.pop("title", None)
    schema.pop("description", None)
    if feedback:
        schema["properties"][FEEDBACK_FIELD] = FEEDBACK_SCHEMA
    return schema


def _describe(exc: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()
    )


def _decode_strings(args: dict[str, Any], keys: set[str]) -> dict[str, Any] | None:
    """``args`` with the JSON-string values of ``keys`` decoded (``None`` if there are none)."""
    decoded = dict(args)
    changed = False
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                decoded[key] = json.loads(value)
                changed = True
            except ValueError:
                pass
    return decoded if changed else None


class Submission[M: BaseModel]:
    """Holds what one phase session submitted."""

    def __init__(
        self,
        name: str,
        model: type[M],
        *,
        description: str,
        check: Check | None = None,
        feedback: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.description = description
        self.check = check
        self.feedback_enabled = feedback
        self.result: M | None = None
        self.feedback = ""
        self.rejections = 0

    def _reject(self, problems: str) -> ToolError:
        self.rejections += 1
        logger.info("%s rejected (%d): %s", self.name, self.rejections, " ".join(problems.split()))
        return ToolError(f"Invalid result, fix it and call {self.name} again:\n{problems}")

    async def _submit(self, args: dict[str, Any]) -> str:
        args = dict(args)
        feedback = str(args.pop(FEEDBACK_FIELD, "") or "").strip()
        try:
            result = self.model.model_validate(args)
        except ValidationError as exc:
            # Models sometimes send a nested object or list as a JSON string.
            decoded = _decode_strings(args, {str(err["loc"][0]) for err in exc.errors() if err["loc"]})
            if decoded is None:
                raise self._reject(_describe(exc)) from exc
            try:
                result = self.model.model_validate(decoded)
            except ValidationError:
                raise self._reject(_describe(exc)) from exc
        if self.check is not None:
            problems = await self.check(result)
            if problems:
                raise self._reject("\n".join(f"- {problem}" for problem in problems))
        self.result = result
        if self.feedback_enabled:
            self.feedback = feedback
        return "Result recorded. You are done."

    def tool(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=submit_schema(self.model, feedback=self.feedback_enabled),
            handler=self._submit,
            terminal=True,
        )
