"""The worker's client for the host-side Claude bridge (`app/llm/claude_bridge.py`)."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.llm import claude_bridge
from app.llm.claude_bridge import ClaudeBridge, ClaudeBridgeError

TOKEN = "t" * 32
SESSION = "0b6f1c1e-3f9e-4a39-9b0c-6a8f8f2f1a11"


def bridge_with(handler) -> ClaudeBridge:
    return ClaudeBridge("http://bridge", TOKEN, timeout=120, transport=httpx.MockTransport(handler))


def chat(bridge: ClaudeBridge, **kw):
    async def run():
        try:
            return await bridge.chat("Hi", model="sonnet", **kw)
        finally:
            await bridge.aclose()

    return asyncio.run(run())


def test_success_sends_the_request():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        payload = {
            "ok": True,
            "text": " Antwort ",
            "session_id": SESSION,
            "model": "claude-sonnet-5-5",
            "cost_usd": 0.1,
        }
        return httpx.Response(200, json=payload)

    reply = chat(bridge_with(handler), session_id=SESSION, system="Sys", max_budget_usd=2.0)
    assert reply == claude_bridge.ClaudeReply("Antwort", SESSION, "claude-sonnet-5-5", 0.1)
    request = seen[0]
    assert request.url.path == "/v1/chat"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body == {
        "prompt": "Hi",
        "model": "sonnet",
        "timeout_s": 120,
        "session_id": SESSION,
        "system": "Sys",
        "max_budget_usd": 2.0,
    }


def raising(exc):
    def handler(request):
        raise exc("boom", request=request)

    return handler


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (raising(httpx.ReadTimeout), "Zeitüberschreitung"),
        (raising(httpx.ConnectError), r"nicht erreichbar \(ConnectError\)"),
        (lambda r: httpx.Response(200, content=b"kein json"), "HTTP 200"),
        (lambda r: httpx.Response(200, json=[1, 2]), "HTTP 200"),
        (lambda r: httpx.Response(502, json={"ok": False, "error": "cli failed"}), "cli failed"),
        (lambda r: httpx.Response(401, json={"error": "unauthorized"}), "unauthorized"),
        (lambda r: httpx.Response(200, json={"ok": True, "text": "  "}), "leere Antwort"),
    ],
)
def test_errors(handler, message):
    with pytest.raises(ClaudeBridgeError, match=message):
        chat(bridge_with(handler))


def settings(tmp_path, token: str | None):
    if token is not None:
        (tmp_path / claude_bridge.TOKEN_SECRET).write_text(token + "\n")
    return SimpleNamespace(secrets_dir=tmp_path, claude_bridge_url="http://bridge:8767")


@pytest.mark.parametrize("token", [None, "", "zu-kurz"])
def test_build_is_off_without_a_usable_token(tmp_path, token):
    assert claude_bridge.build_claude_bridge(settings(tmp_path, token)) is None


def test_build(tmp_path):
    bridge = claude_bridge.build_claude_bridge(settings(tmp_path, TOKEN), timeout=300)
    assert bridge is not None
    assert bridge.token == TOKEN and bridge.timeout == 300
    asyncio.run(bridge.aclose())
