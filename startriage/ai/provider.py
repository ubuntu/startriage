"""Provider abstraction: run one agent session with host tools.

The host decides everything about a session (see :class:`SessionSpec`): its
system prompt, its task, and the only tools the model may call. The Copilot
CLI's built-in shell/file/web tools are never exposed.

Where the credential goes differs per provider:

- **Copilot** authenticates the CLI process itself, so its GitHub token is a
  ``CopilotClient(...)`` kwarg (see :func:`build_client_kwargs`).
- **OpenRouter** is BYOK through the same loop, supplied as the
  ``create_session(provider=...)`` kwarg (see :func:`build_session_kwargs`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AI_SETUP_HINT, AIConfig, AIConfigError, AIProvider
from .supervisor import ABORTED, MAX_CALLS, Supervisor
from .tools import ToolSpec, call_tool

logger = logging.getLogger(__name__)

# Upper bound for one model turn (task or nudge), tool calls included.
TURN_TIMEOUT = 900


@dataclass
class SessionSpec:
    """One agent session, as decided by the host."""

    system_prompt: str
    task: str
    tools: list[ToolSpec]
    working_directory: Path | None = None
    # Tool calls allowed, submit tools excepted (see Supervisor).
    max_calls: int = MAX_CALLS
    # Sent once if the first turn ends while ``is_done()`` is still false.
    nudge: str | None = None
    is_done: Callable[[], bool] = lambda: True


@dataclass
class SessionResult:
    text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    nudged: bool = False
    aborted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class Provider(ABC):
    """A backend capable of running one agent session."""

    #: Model id passed to the underlying session.
    model: str

    @abstractmethod
    async def run(self, spec: SessionSpec) -> SessionResult:
        raise NotImplementedError


def build_client_kwargs(ai_config: AIConfig) -> dict[str, Any]:
    """Build the ``CopilotClient(...)`` kwargs for ``ai_config``.

    For the Copilot provider this carries the GitHub token that authenticates the
    CLI process (optional here — the SDK also reads it from the environment). For
    OpenRouter (BYOK) the credential travels on the session instead, so no client
    auth is needed. The token is resolved with config-over-env precedence via
    :meth:`AIConfig.resolve_token`.
    """
    match ai_config.provider:
        case AIProvider.copilot:
            token = ai_config.resolve_token()
            if token:
                return {"github_token": token}
            return {}
        case _:
            return {}


def build_session_kwargs(ai_config: AIConfig) -> dict[str, Any]:
    """Build the ``create_session(...)`` provider kwargs for ``ai_config``.

    Only OpenRouter (BYOK) contributes here, as an OpenAI-compatible ``provider``
    block; the Copilot provider authenticates at the client level instead.
    """
    match ai_config.provider:
        case AIProvider.openrouter:
            return {
                "provider": {
                    "type": "openai",
                    "base_url": ai_config.openrouter_base_url,
                    "api_key": ai_config.resolve_token(),
                }
            }
        case _:
            return {}


def session_options(spec: SessionSpec, ai_config: AIConfig) -> dict[str, Any]:
    """``create_session(...)`` kwargs that do not need the SDK."""
    options: dict[str, Any] = {
        "model": ai_config.model,
        # Only our tools: naming them hides every built-in CLI tool.
        "available_tools": [tool.name for tool in spec.tools],
        # "replace" drops the CLI's ~12KB coding-agent prompt; we need none of it.
        "system_message": {"mode": "replace", "content": spec.system_prompt},
        # Nothing from the user's environment leaks into the session.
        "skip_custom_instructions": True,
        "enable_config_discovery": False,
        "enable_skills": False,
        **build_session_kwargs(ai_config),
    }
    if spec.working_directory is not None:
        options["working_directory"] = str(spec.working_directory)
    return options


def _event_type(event: Any) -> str:
    return getattr(event.type, "value", str(event.type))


class CopilotProvider(Provider):
    """Real provider backed by the Copilot Python SDK (lazily imported).

    The SDK (and the Node Copilot CLI it spawns) is imported only when a session is
    actually run, so non-AI commands and offline tests never need it installed.
    """

    def __init__(self, ai_config: AIConfig) -> None:
        if not ai_config.model:
            raise AIConfigError(AI_SETUP_HINT)
        self._ai_config = ai_config
        self.model = ai_config.model

    async def run(self, spec: SessionSpec) -> SessionResult:
        from copilot import CopilotClient  # ty: ignore[unresolved-import]
        from copilot.rpc import PermissionDecisionReject  # ty: ignore[unresolved-import]
        from copilot.tools import Tool, ToolResult  # ty: ignore[unresolved-import]

        result = SessionResult()
        supervisor = Supervisor(
            max_calls=spec.max_calls, exempt=frozenset(t.name for t in spec.tools if t.terminal)
        )
        aborted = asyncio.Event()

        def sdk_tool(tool: ToolSpec) -> Any:
            async def handler(invocation: Any) -> Any:
                result.tool_calls.append(tool.name)
                text, ok = await call_tool(tool, invocation.arguments or {})
                if ok:
                    return ToolResult(text_result_for_llm=text)
                return ToolResult(text_result_for_llm=text, result_type="failure", error=text)

            return Tool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                handler=handler,
                skip_permission=True,
                is_terminal=tool.terminal,
            )

        async def on_pre_tool_use(hook_input: Any, _invocation: Any) -> Any:
            # The SDK runs the tool if a hook raises, so never let one escape.
            try:
                logger.debug("Tool call: %s %s", hook_input["toolName"], hook_input["toolArgs"])
                reason = supervisor.check(hook_input["toolName"], hook_input["toolArgs"])
            except Exception:
                logger.exception("AI supervisor failed, aborting")
                supervisor.aborted = True
                reason = ABORTED
            if supervisor.aborted:
                aborted.set()
            if reason is None:
                return None
            return {"permissionDecision": "deny", "permissionDecisionReason": reason}

        def on_event(event: Any) -> None:
            etype = _event_type(event)
            logger.debug("Copilot session event: %s", etype)
            if etype == "assistant.usage":
                result.input_tokens += getattr(event.data, "input_tokens", 0) or 0
                result.output_tokens += getattr(event.data, "output_tokens", 0) or 0

        def reject(_request: Any, _invocation: Any) -> Any:
            # Our tools skip permission; anything else asking is not allowed.
            return PermissionDecisionReject(feedback="Not allowed.")

        async def send(session: Any, prompt: str) -> Any:
            """Send ``prompt`` and wait for idle, or stop early if the supervisor aborts."""
            sending = asyncio.ensure_future(session.send_and_wait(prompt, timeout=TURN_TIMEOUT))
            stopping = asyncio.ensure_future(aborted.wait())
            await asyncio.wait({sending, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if sending.done():
                return sending.result()
            sending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sending
            return None

        logger.debug(
            "Starting Copilot session (model=%s, tools=%s)", self.model, [t.name for t in spec.tools]
        )
        async with CopilotClient(**build_client_kwargs(self._ai_config)) as client:
            async with await client.create_session(
                on_permission_request=reject,
                tools=[sdk_tool(tool) for tool in spec.tools],
                hooks={"on_pre_tool_use": on_pre_tool_use},
                **session_options(spec, self._ai_config),
            ) as session:
                session.on(on_event)
                message = await send(session, spec.task)
                if not aborted.is_set() and spec.nudge and not spec.is_done():
                    logger.info("No result submitted, nudging the model once")
                    result.nudged = True
                    message = await send(session, spec.nudge)
                result.text = (message.data.content or "") if message else ""
        result.aborted = aborted.is_set()
        return result


@dataclass
class FakeTurn:
    """What the fake model does in one turn: some tool calls, then a final text."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class FakeProvider(Provider):
    """Deterministic in-memory provider for offline tests.

    Each queued session is a list of turns: the first answers the task, the
    second (if any) answers the nudge. Tool calls go through the real tools and
    supervisor. Every spec is recorded on :attr:`specs` and every tool call on
    :attr:`calls` as ``(name, args, output, succeeded)``.
    """

    def __init__(self, sessions: list[list[FakeTurn]] | None = None, *, model: str = "fake-model") -> None:
        self.model = model
        self._sessions = list(sessions or [])
        self.specs: list[SessionSpec] = []
        self.calls: list[tuple[str, dict[str, Any], str, bool]] = []

    async def run(self, spec: SessionSpec) -> SessionResult:
        self.specs.append(spec)
        turns = self._sessions.pop(0) if self._sessions else []
        tools = {tool.name: tool for tool in spec.tools}
        supervisor = Supervisor(
            max_calls=spec.max_calls, exempt=frozenset(t.name for t in spec.tools if t.terminal)
        )
        result = SessionResult()
        for index, turn in enumerate(turns):
            if index == 1:
                if not spec.nudge or spec.is_done():
                    break
                result.nudged = True
            elif index > 1:
                break
            result.input_tokens += turn.input_tokens
            result.output_tokens += turn.output_tokens
            for name, args in turn.calls:
                result.tool_calls.append(name)
                reason = supervisor.check(name, args)
                if reason is not None:
                    self.calls.append((name, args, reason, False))
                    if supervisor.aborted:
                        result.aborted = True
                        return result
                    continue
                text, ok = await call_tool(tools[name], args)
                self.calls.append((name, args, text, ok))
                if ok and tools[name].terminal:
                    break
            result.text = turn.text
        return result


def build_provider(ai_config: AIConfig) -> Provider:
    """Return a ready provider for ``ai_config``, validating the backend first.

    The readiness check lives on :class:`AIConfig` as a context-gated model
    validator; re-validating here with ``require_ai`` context runs it at the AI
    entry point, raising :class:`~startriage.config.AIConfigError` when no
    provider/model has been chosen, or when the active provider has no usable
    credential (from config or env), so misconfig fails before any session is
    started.
    """
    AIConfig.model_validate(ai_config.model_dump(), context={"require_ai": True})
    return CopilotProvider(ai_config)
