"""Provider abstraction over the Copilot SDK for agentic triage.

The Copilot CLI is itself the agent loop (built-in shell/file/web tools plus its
own tool-calling loop), so a "provider" is deliberately thin: it only starts a
session with the right auth/model and returns the agent's final assistant message.

The only thing that differs between providers is *where* the credential goes:

- **Copilot** authenticates the CLI process itself, so its GitHub token is a
  ``CopilotClient(...)`` kwarg (see :func:`build_client_kwargs`).
- **OpenRouter** is BYOK through the same loop, supplied as the
  ``create_session(provider=...)`` kwarg (see :func:`build_session_kwargs`).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from ..config import AIConfig
from ..enums import AIPermission, AIProvider

logger = logging.getLogger(__name__)

# Appended to the system prompt in restricted mode, where every tool call is
# rejected. Without it the agent would keep trying to run commands and only
# discover they are denied one failure at a time.
RESTRICTED_PROMPT_NOTE = (
    "\n\nTool execution is disabled for this run. Reason only over the bug metadata "
    "provided in the user message; do not attempt to run shell commands, read or write "
    "files, or fetch URLs. If a step would require executing a tool, say so and explain "
    "what you would have done instead of trying."
)


class Provider(ABC):
    """A backend capable of running one agent session and returning its final text."""

    #: Model id passed to the underlying session.
    model: str

    @abstractmethod
    async def run(self, system_prompt: str, user_message: str) -> str:
        """Run a single agent session and return the final assistant text."""
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


def _log_session_event(event: Any) -> None:
    """Log a Copilot session step event at DEBUG (subscribed only under -vv)."""
    event_type = getattr(event, "type", None) or type(event).__name__
    logger.debug("Copilot session event: %s", event_type)


class CopilotProvider(Provider):
    """Real provider backed by the Copilot Python SDK (lazily imported).

    The SDK (and the Node Copilot CLI it spawns) is imported only when a session is
    actually run, so non-AI commands and offline tests never need it installed.
    The ``permission`` level decides how tool calls are handled: ``restricted``
    rejects every tool (text-only reasoning), ``full`` auto-approves them, and
    ``ask`` prompts on the terminal before each call.
    """

    def __init__(self, ai_config: AIConfig, permission: AIPermission) -> None:
        self._ai_config = ai_config
        self._permission = permission
        self.model = ai_config.model

    async def run(self, system_prompt: str, user_message: str) -> str:
        # Lazy import keeps the SDK (and the Node CLI it spawns) optional; it is
        # bundled by the snap rather than declared as a hard Python dependency.
        from copilot import CopilotClient  # ty: ignore[unresolved-import]

        if self._permission is AIPermission.restricted:
            system_prompt += RESTRICTED_PROMPT_NOTE

        logger.debug(
            "Starting Copilot session (model=%s, permission=%s)", self.model, self._permission
        )
        async with CopilotClient(**build_client_kwargs(self._ai_config)) as client:
            async with await client.create_session(
                on_permission_request=build_permission_handler(self._permission),
                model=self.model,
                # "append" keeps the CLI's tool-use foundation and layers our
                # behavioural prompt on top ("replace" would drop its guardrails).
                system_message={"mode": "append", "content": system_prompt},
                **build_session_kwargs(self._ai_config),
            ) as session:
                # At -vv, stream the agent's step events (tool calls, reasoning)
                # so unattended runs are auditable; cheap no-op otherwise.
                if logger.isEnabledFor(logging.DEBUG):
                    session.on(_log_session_event)
                # timeout=None waits until the agent is idle rather than aborting
                # after the SDK's 60s default; triage turns routinely run longer
                # (source pulls, debdiffs). The user can cancel with Ctrl-C.
                message = await session.send_and_wait(user_message, timeout=None)
                return (message.data.content or "") if message else ""


class FakeProvider(Provider):
    """Deterministic in-memory provider for offline tests.

    Returns queued ``responses`` in order, falling back to ``default_response`` once
    the queue is drained, and records every ``(system_prompt, user_message)`` call
    on :attr:`calls` for assertions.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        model: str = "fake-model",
        default_response: str = "",
    ) -> None:
        self.model = model
        self._responses = list(responses or [])
        self._default_response = default_response
        self.calls: list[tuple[str, str]] = []

    async def run(self, system_prompt: str, user_message: str) -> str:
        self.calls.append((system_prompt, user_message))
        if self._responses:
            return self._responses.pop(0)
        return self._default_response


def build_permission_handler(permission: AIPermission) -> Any:
    """Return the Copilot ``on_permission_request`` handler for ``permission``.

    - ``full`` auto-approves every tool call (:meth:`PermissionHandler.approve_all`).
    - ``restricted`` rejects every tool call, so the agent can only reason over the
      metadata it was given.
    - ``ask`` prompts on the terminal and approves or rejects each call.

    The Copilot SDK is imported lazily so non-AI commands and offline tests never
    need it installed. Passing ``None`` (the SDK's "leave pending" mode) is
    deliberately avoided: our runs drive the session with ``send_and_wait`` and do
    not resolve pending permission RPCs, so an unanswered request would hang.
    """
    from copilot.rpc import (  # ty: ignore[unresolved-import]
        PermissionDecisionApproveOnce,
        PermissionDecisionReject,
    )
    from copilot.session import PermissionHandler  # ty: ignore[unresolved-import]

    match permission:
        case AIPermission.full:
            return PermissionHandler.approve_all
        case AIPermission.restricted:
            def reject(request: Any, invocation: Any) -> Any:
                return PermissionDecisionReject(feedback="Tool execution is disabled for this run.")

            return reject
        case AIPermission.ask:
            async def ask(request: Any, invocation: Any) -> Any:
                summary = getattr(request, "full_command_text", None) or type(request).__name__
                answer = await asyncio.to_thread(input, f"Allow the agent to run: {summary}? [y/N] ")
                if answer.strip().lower() in ("y", "yes"):
                    return PermissionDecisionApproveOnce()
                return PermissionDecisionReject(feedback="User denied this action.")

            return ask
        case _:
            raise RuntimeError("unhandled permission")


def build_provider(ai_config: AIConfig, permission: AIPermission) -> Provider:
    """Return a ready provider for ``ai_config``, validating credentials first.

    The credential check lives on :class:`AIConfig` as a context-gated model
    validator; re-validating here with ``require_ai`` context runs it at the AI
    entry point, raising :class:`~startriage.config.AIConfigError` when the active
    provider has no usable credential (from config or env) so misconfig fails
    before any session is started. ``permission`` decides how the agent's tool
    calls are handled at run time (see :func:`build_permission_handler`).
    """
    AIConfig.model_validate(ai_config.model_dump(), context={"require_ai": True})
    return CopilotProvider(ai_config, permission)
