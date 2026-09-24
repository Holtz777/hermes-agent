"""Cursor ACP provider profile.

Drives ``cursor-agent acp`` over stdio through the Copilot ACP shim, with the Cursor-specific
differences handled in :class:`CursorACPClient`:

* ``session/new`` opens in Cursor's ``agent`` mode, where the CLI edits files and runs shell
  commands itself without ever sending ``session/request_permission`` — so the shim's
  "cancel every permission" guard does not hold. Each session is switched to ``ask`` (read-only)
  before the prompt, leaving tool execution to Hermes via ``<tool_call>`` blocks.
* ``cursor/*`` extension notifications (no ``id``) are swallowed instead of answered.
* Auth is owned by the CLI (``cursor-agent login`` or ``CURSOR_API_KEY``); a logged-in CLI
  needs no ACP ``authenticate`` call.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

_DEFAULT_MODE = "ask"
# Replaces the shim's "Use ACP capabilities" preamble: in ask mode Cursor otherwise refuses
# ("switch to Agent mode") instead of handing the action back to Hermes as a tool call.
_ASK_MODE_PREAMBLE = (
    "You are the language model behind Hermes, a separate agent host that owns every tool.",
    "This Cursor session is read-only on purpose: do NOT use Cursor's own tools and never ask the user to switch modes.",
    "To take an action, output tool calls as <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI "
    "function-call shape, using only the tools listed below. Hermes runs them and returns the results next turn.",
    "If no tool is needed, answer normally.",
)


def _client_class() -> type:
    from agent.copilot_acp_client import _PROMPT_PREAMBLE, CopilotACPClient

    shim_preamble = "\n\n".join(_PROMPT_PREAMBLE)

    class CursorACPClient(CopilotACPClient):
        """Copilot ACP shim adapted to ``cursor-agent acp``."""

        def __init__(self, **kwargs: Any):
            super().__init__(**kwargs)
            self.api_key = kwargs.get("api_key") or "cursor-acp"
            # "agent" restores Cursor's own tool execution (edits/shell run inside the CLI).
            self._mode = os.getenv("HERMES_CURSOR_ACP_MODE", "").strip() or _DEFAULT_MODE

        @contextlib.contextmanager
        def _session(self, timeout_seconds: float, *, allow_file_requests: bool = True):
            with super()._session(timeout_seconds, allow_file_requests=allow_file_requests) as (session, request):
                session_id = str(session.get("sessionId") or "").strip()
                if self._mode != "agent":
                    request("session/set_mode", {"sessionId": session_id, "modeId": self._mode})
                yield session, request

        def _run_prompt(self, prompt_text: str, **kwargs: Any) -> tuple[str, str]:
            if self._mode != "agent" and prompt_text.startswith(shim_preamble):
                prompt_text = "\n\n".join(_ASK_MODE_PREAMBLE) + prompt_text[len(shim_preamble):]
            return super()._run_prompt(prompt_text, **kwargs)

        def _handle_server_message(self, msg: dict[str, Any], **kwargs: Any) -> bool:
            method = msg.get("method")
            if isinstance(method, str) and method.startswith("cursor/") and "id" not in msg:
                return True
            return super()._handle_server_message(msg, **kwargs)

    return CursorACPClient


class CursorACPProfile(ProviderProfile):
    """Cursor ACP — external process, models come from the signed-in session."""

    def create_client(self, **client_kwargs: Any) -> Any:
        return _client_class()(**client_kwargs)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0
    ) -> list[str] | None:
        """Model ids advertised by a short-lived ``session/new`` (``api_key``/``base_url`` ignored)."""
        from hermes_cli.auth import resolve_external_process_provider_credentials

        try:
            creds = resolve_external_process_provider_credentials(self.name)
            if not str(creds.get("base_url") or "").startswith("acp://"):
                return None
            client = self.create_client(
                api_key=creds.get("api_key"), base_url=creds.get("base_url"),
                command=creds.get("command"), args=creds.get("args"))
            return client.list_models(timeout_seconds=timeout) or None
        except Exception:
            return None


cursor_acp = CursorACPProfile(
    name="cursor-acp", aliases=("cursor", "cursor-agent"),
    display_name="Cursor (ACP)",
    description="Cursor subscription via `cursor-agent acp` (run `cursor-agent login` first)",
    signup_url="https://cursor.com/cli",
    api_mode="chat_completions",
    env_vars=(),
    base_url="acp://cursor",
    auth_type="external_process",
    process_command="cursor-agent",
    process_args=("acp",),
    process_command_env_vars=("HERMES_CURSOR_ACP_COMMAND",),
    process_args_env_var="HERMES_CURSOR_ACP_ARGS",
)

register_provider(cursor_acp)
