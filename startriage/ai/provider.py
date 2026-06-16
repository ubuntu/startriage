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

from abc import ABC, abstractmethod
from typing import Any

from ..config import AIConfig
from ..enums import AIProvider


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
    if ai_config.provider is AIProvider.copilot:
        token = ai_config.resolve_token()
        if token:
            return {"github_token": token}
    return {}


def build_session_kwargs(ai_config: AIConfig) -> dict[str, Any]:
    """Build the ``create_session(...)`` provider kwargs for ``ai_config``.

    Only OpenRouter (BYOK) contributes here, as an OpenAI-compatible ``provider``
    block; the Copilot provider authenticates at the client level instead.
    """
    if ai_config.provider is AIProvider.openrouter:
        return {
            "provider": {
                "type": "openai",
                "base_url": ai_config.openrouter_base_url,
                "api_key": ai_config.resolve_token(),
            }
        }
    return {}


class CopilotProvider(Provider):
    """Real provider backed by the Copilot Python SDK (lazily imported).

    The SDK (and the Node Copilot CLI it spawns) is imported only when a session is
    actually run, so non-AI commands and offline tests never need it installed.
    All tools are auto-approved so unattended runs never block on a prompt; the
    safety boundary is snap confinement plus a dedicated scratch dir, not an
    allow-list.
    """

    def __init__(self, ai_config: AIConfig) -> None:
        self._ai_config = ai_config
        self.model = ai_config.model

    async def run(self, system_prompt: str, user_message: str) -> str:
        # Lazy import keeps the SDK (and the Node CLI it spawns) optional; it is
        # bundled by the snap rather than declared as a hard Python dependency.
        from copilot import CopilotClient  # ty: ignore[unresolved-import]
        from copilot.session import PermissionHandler  # ty: ignore[unresolved-import]

        async with CopilotClient(**build_client_kwargs(self._ai_config)) as client:
            async with await client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                model=self.model,
                # "append" keeps the CLI's tool-use foundation and layers our
                # behavioural prompt on top ("replace" would drop its guardrails).
                system_message={"mode": "append", "content": system_prompt},
                **build_session_kwargs(self._ai_config),
            ) as session:
                message = await session.send_and_wait(user_message)
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


def build_provider(ai_config: AIConfig) -> Provider:
    """Return a ready provider for ``ai_config``, validating credentials first.

    Raises :class:`AIConfigError` (via :meth:`AIConfig.require_configured`) when the
    active provider has no usable credential, so callers fail smoothly before any
    session is started.
    """
    ai_config.require_configured()
    return CopilotProvider(ai_config)
