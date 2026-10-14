"""The worker's MCP endpoint with the read-only Kickbase tools for Claude."""

import asyncio
import socket

import pytest
from starlette.testclient import TestClient

from app.bot import mcp_server

TOKEN = "s" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeTools:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return f"Ergebnis {name}", False


def client(tools=None) -> TestClient:
    return TestClient(mcp_server.build_app(tools or FakeTools(), TOKEN))


def rpc(method, msg_id=1, **params) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}


def test_rejects_missing_or_wrong_token():
    c = client()
    assert c.post("/mcp", json=rpc("ping")).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert c.post("/mcp", json=rpc("ping"), headers=bad).status_code == 401


def test_empty_token_never_authorizes():
    c = TestClient(mcp_server.build_app(FakeTools(), ""))
    assert c.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer "}).status_code == 401


def test_rejects_browser_origin():
    headers = {**AUTH, "Origin": "http://evil.example"}
    assert client().post("/mcp", json=rpc("ping"), headers=headers).status_code == 403


def test_only_post():
    resp = client().get("/mcp", headers=AUTH)
    assert resp.status_code == 405 and resp.headers["allow"] == "POST"


def test_parse_error():
    resp = client().post("/mcp", content=b"{", headers=AUTH)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32700


def test_initialize_negotiates_version():
    resp = client().post("/mcp", json=rpc("initialize", protocolVersion="2025-03-26"), headers=AUTH)
    result = resp.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "tools" in result["capabilities"]
    unknown = client().post("/mcp", json=rpc("initialize", protocolVersion="1999"), headers=AUTH)
    assert unknown.json()["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL


def test_tools_list_and_call():
    tools = FakeTools()
    c = client(tools)
    listed = c.post("/mcp", json=rpc("tools/list"), headers=AUTH).json()["result"]["tools"]
    assert {t["name"] for t in listed} == {"lagebild", "spieler", "markt", "mein_kader", "budget"}
    call = rpc("tools/call", name="spieler", arguments={"name_oder_id": "Kane"})
    result = c.post("/mcp", json=call, headers=AUTH).json()["result"]
    assert result == {"content": [{"type": "text", "text": "Ergebnis spieler"}], "isError": False}
    assert tools.calls == [("spieler", {"name_oder_id": "Kane"})]


def test_unknown_tool_and_method():
    c = client()
    unknown_tool = c.post("/mcp", json=rpc("tools/call", name="bieten"), headers=AUTH).json()
    assert unknown_tool["error"]["code"] == -32602
    unknown_method = c.post("/mcp", json=rpc("resources/list"), headers=AUTH).json()
    assert unknown_method["error"]["code"] == -32601


def test_notification_and_batch():
    c = client()
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert c.post("/mcp", json=note, headers=AUTH).status_code == 202
    assert c.post("/mcp", json=[note], headers=AUTH).status_code == 202
    batch = c.post("/mcp", json=[note, rpc("ping", 7), {"id": 8}], headers=AUTH).json()
    assert batch[0] == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert batch[1]["error"]["code"] == -32600


def test_kickbase_tools_never_raise(monkeypatch):
    class League:
        async def get(self):
            raise RuntimeError("Kickbase down")

    class Ctx:
        league = League()

    tools = mcp_server.KickbaseTools(Ctx())
    text, is_error = asyncio.run(tools.call("budget", {}))
    assert is_error and "RuntimeError" in text
    text, is_error = asyncio.run(tools.call("spieler", {}))
    assert is_error and "fehlt" in text


def test_situation_is_cached(monkeypatch):
    built: list[int] = []

    class Situation:
        text = "LAGE"

    async def fake_build(tool_ctx, tz):
        built.append(1)
        return Situation()

    class League:
        async def get(self):
            return object()

    class Ctx:
        league = League()
        client = sessions = oldb = None
        tz = None

    now = [0.0]
    monkeypatch.setattr(mcp_server, "build_situation", fake_build)
    tools = mcp_server.KickbaseTools(Ctx(), clock=lambda: now[0])

    async def run():
        assert await tools.call("lagebild", {}) == ("LAGE", False)
        await tools.call("lagebild", {})
        now[0] = mcp_server.SITUATION_TTL + 1
        await tools.call("lagebild", {})

    asyncio.run(run())
    assert len(built) == 2


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_mcp_starts_and_stops():
    port = free_port()

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(
            mcp_server.serve_mcp(None, TOKEN, port, stop, host="127.0.0.1", tools=FakeTools())
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                continue
            writer.close()
            break
        else:
            raise AssertionError("MCP-Server nicht erreichbar")
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(run())


def test_serve_mcp_port_in_use_raises_instead_of_exiting():
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]

        async def run():
            stop = asyncio.Event()
            await mcp_server.serve_mcp(None, TOKEN, port, stop, host="127.0.0.1", tools=FakeTools())

        with pytest.raises(RuntimeError, match=str(port)):
            asyncio.run(run())
