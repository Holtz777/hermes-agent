"""cursor-acp provider plugin: registration and the Cursor-specific ACP session quirks."""

from __future__ import annotations

import io
import json
import sys

from providers import get_provider_profile

_FAKE_CURSOR = """import json
import sys

log = open(sys.argv[1], "a")
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    log.write(method + "\\n"); log.flush()
    if method == "initialize":
        result = {"protocolVersion": 1, "authMethods": [{"id": "cursor_login"}]}
    elif method == "session/new":
        result = {"sessionId": "s1", "configOptions": [{"id": "model", "category": "model",
                  "options": [{"value": "default[]"}, {"value": "composer-2.5[fast=true]"}]}]}
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc": "2.0", "method": "cursor/update_todos", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
              "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "PONG"}}}}),
              flush=True)
        result = {"stopReason": "end_turn"}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


def _client(tmp_path, monkeypatch, mode: str | None = None):
    if mode is None:
        monkeypatch.delenv("HERMES_CURSOR_ACP_MODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_CURSOR_ACP_MODE", mode)
    server = tmp_path / "fake_cursor_acp.py"
    server.write_text(_FAKE_CURSOR, encoding="utf-8")
    log = tmp_path / "methods.log"
    client = get_provider_profile("cursor-acp").create_client(
        command=sys.executable, args=[str(server), str(log)], acp_cwd=str(tmp_path))
    return client, log


def test_profile_registered_as_external_process():
    profile = get_provider_profile("cursor-acp")
    assert profile is not None
    assert profile.auth_type == "external_process"
    assert profile.base_url == "acp://cursor"
    assert (profile.process_command, tuple(profile.process_args)) == ("cursor-agent", ("acp",))
    assert get_provider_profile("cursor") is profile


def test_session_switches_to_ask_mode_before_prompt(tmp_path, monkeypatch):
    client, log = _client(tmp_path, monkeypatch)
    text, _ = client._run_prompt("hi", timeout_seconds=30)
    assert text == "PONG"
    assert log.read_text().split() == ["initialize", "session/new", "session/set_mode", "session/prompt"]


def test_agent_mode_skips_set_mode(tmp_path, monkeypatch):
    client, log = _client(tmp_path, monkeypatch, mode="agent")
    client._run_prompt("hi", timeout_seconds=30)
    assert "session/set_mode" not in log.read_text().split()


def test_list_models_reads_cursor_session_options(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    assert client.list_models(timeout_seconds=30) == ["default[]", "composer-2.5[fast=true]"]


def test_cursor_notifications_are_not_answered(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    process = _FakeProcess()
    handled = client._handle_server_message(
        {"jsonrpc": "2.0", "method": "cursor/update_todos", "params": {}},
        process=process, cwd=str(tmp_path), text_parts=[], reasoning_parts=[])
    assert handled is True
    assert process.stdin.getvalue() == ""


def test_cursor_requests_still_get_a_reply(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    process = _FakeProcess()
    client._handle_server_message(
        {"jsonrpc": "2.0", "id": 7, "method": "cursor/ask_question", "params": {}},
        process=process, cwd=str(tmp_path), text_parts=[], reasoning_parts=[])
    reply = json.loads(process.stdin.getvalue())
    assert reply["id"] == 7 and "error" in reply


def test_ask_mode_replaces_shim_preamble(tmp_path, monkeypatch):
    from agent.copilot_acp_client import CopilotACPClient, _format_messages_as_prompt

    seen = {}
    monkeypatch.setattr(CopilotACPClient, "_run_prompt",
                        lambda self, prompt_text, **_: seen.setdefault("prompt", prompt_text) and ("", ""))
    client, _ = _client(tmp_path, monkeypatch)
    client._run_prompt(_format_messages_as_prompt([{"role": "user", "content": "hi"}]), timeout_seconds=5)
    assert seen["prompt"].startswith("You are the language model behind Hermes")
    assert "Use ACP capabilities" not in seen["prompt"]
    assert seen["prompt"].rstrip().endswith("Continue the conversation from the latest user request.")
