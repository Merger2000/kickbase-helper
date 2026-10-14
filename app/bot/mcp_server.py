"""Read-only Kickbase tools for Claude as a minimal MCP server (Streamable HTTP, JSON only).

The Claude bridge on the host (`deploy/claude-bridge/bridge.py`) points the Claude Code CLI at
this endpoint. It runs inside the worker, reuses the bot's tools (`app/bot/tools.py`) and the
Lagebild (`app/bot/context.py`) and never changes anything at Kickbase. Published only on the
host's loopback (compose: `127.0.0.1:8768`) and protected by the bridge token.
"""

import asyncio
import hmac
import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.bot.context import build_situation
from app.bot.tools import ToolContext, run_tool

logger = logging.getLogger("app.bot.mcp")

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL = "2025-06-18"
SITUATION_TTL = 120.0  # seconds: one conversation round reuses the same Lagebild
MAX_ARGUMENT = 120

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_DEFS = [
    {
        "name": "lagebild",
        "description": (
            "Aktuelles Lagebild des Nutzers in seiner Kickbase-Liga: Deadline, Kontostand, "
            "Aufstellung, Kader mit Scores, Verkaufs- und Kaufempfehlungen, nächster Spieltag. "
            "Stand jetzt, aus dem Dashboard."
        ),
        "inputSchema": _NO_ARGS,
    },
    {
        "name": "spieler",
        "description": (
            "Details zu einem Bundesliga-Spieler: Verein, Position, Marktwert und Trend, Form, "
            "Status, Startchance, Score mit Bestandteilen, aktuelle Meldungen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name_oder_id": {
                    "type": "string",
                    "description": "Spielername (z. B. 'Kane') oder Kickbase-Spieler-ID",
                }
            },
            "required": ["name_oder_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "markt",
        "description": (
            "Kaufkandidaten auf dem Transfermarkt der Liga, nach Score sortiert. Suche als Text, "
            "z. B. 'ST bis 5 Mio', 'Abwehr 10', 'alle'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "suche": {"type": "string", "description": "Position und/oder Höchstpreis in Mio."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mein_kader",
        "description": "Alle eigenen Spieler mit Position, Marktwert und Status.",
        "inputSchema": _NO_ARGS,
    },
    {
        "name": "budget",
        "description": "Kontostand und Kontostand nach allen offenen Geboten.",
        "inputSchema": _NO_ARGS,
    },
]
TOOL_NAMES = {t["name"] for t in TOOL_DEFS}


class KickbaseTools:
    """Executes the MCP tools with the worker's JobContext."""

    def __init__(self, ctx, clock=time.monotonic) -> None:
        self._ctx = ctx
        self._clock = clock
        self._lock = asyncio.Lock()
        self._situation: tuple[float, Any] | None = None

    async def _tool_context(self) -> ToolContext:
        ctx = self._ctx
        league = await ctx.league.get()
        return ToolContext(ctx.client, ctx.sessions, league, ctx.oldb, datetime.now(UTC))

    async def _with_situation(self) -> ToolContext:
        """ToolContext with a (briefly cached) Lagebild; building it takes several queries."""
        async with self._lock:
            tool_ctx = await self._tool_context()
            cached = self._situation
            if cached is None or self._clock() - cached[0] > SITUATION_TTL:
                cached = (self._clock(), await build_situation(tool_ctx, self._ctx.tz))
                self._situation = cached
            tool_ctx.situation = cached[1]
            return tool_ctx

    async def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """(text, is_error). Never raises: errors become a short message for the model."""
        try:
            match name:
                case "lagebild":
                    return (await self._with_situation()).situation.text, False
                case "markt":
                    query = _argument(arguments, "suche") or "alle"
                    return await run_tool(await self._with_situation(), "markt", query), False
                case "spieler":
                    who = _argument(arguments, "name_oder_id")
                    if not who:
                        return "name_oder_id fehlt.", True
                    return await run_tool(await self._tool_context(), "spieler", who), False
                case "mein_kader" | "budget":
                    return await run_tool(await self._tool_context(), name, None), False
                case _:
                    return f"Unbekanntes Werkzeug '{name}'.", True
        except Exception as exc:  # the data may be unavailable; Claude should still answer
            logger.warning("MCP-Werkzeug %s fehlgeschlagen: %s", name, type(exc).__name__)
            return f"Werkzeug '{name}' gerade nicht verfügbar ({type(exc).__name__}).", True


def _argument(arguments: dict, key: str) -> str:
    value = arguments.get(key) if isinstance(arguments, dict) else None
    return str(value).strip()[:MAX_ARGUMENT] if value is not None else ""


def _result(msg_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle_message(message: Any, tools: KickbaseTools) -> dict | None:
    """One JSON-RPC message → response (None for notifications)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message:
        return None  # notification (e.g. notifications/initialized)
    msg_id = message["id"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "Invalid params")
    match method:
        case "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL
            return _result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "kickbase-helper", "version": "1.0"},
                    "instructions": (
                        "Nur lesende Daten aus dem Kickbase-Dashboard des Nutzers. Für Fragen zu "
                        "Kader, Aufstellung, Budget, Markt und Spielern zuerst lagebild aufrufen."
                    ),
                },
            )
        case "ping":
            return _result(msg_id, {})
        case "tools/list":
            return _result(msg_id, {"tools": TOOL_DEFS})
        case "tools/call":
            name = params.get("name")
            if name not in TOOL_NAMES:
                return _error(msg_id, -32602, f"Unknown tool: {name}")
            text, is_error = await tools.call(name, params.get("arguments") or {})
            content = [{"type": "text", "text": text}]
            return _result(msg_id, {"content": content, "isError": is_error})
        case _:
            return _error(msg_id, -32601, "Method not found")


def build_app(tools: KickbaseTools, token: str) -> Starlette:
    expected = f"Bearer {token}".encode()

    async def mcp(request: Request) -> Response:
        auth = request.headers.get("authorization", "").encode()
        if not token or not hmac.compare_digest(auth, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.headers.get("origin"):
            # Only the CLI on the host talks to us, never a browser (DNS rebinding).
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
        if isinstance(body, list):
            responses = [r for m in body if (r := await handle_message(m, tools)) is not None]
            return JSONResponse(responses) if responses else Response(status_code=202)
        response = await handle_message(body, tools)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The worker handles SIGTERM itself (loop.add_signal_handler) and stops us via `stop`.
        yield


async def _serve(server: uvicorn.Server, port: int) -> None:
    # uvicorn calls sys.exit(1) when it cannot bind; SystemExit must not end the whole worker.
    try:
        await server.serve()
    except SystemExit:
        raise RuntimeError(f"MCP-Server konnte Port {port} nicht öffnen") from None


async def serve_mcp(
    ctx, token: str, port: int, stop: asyncio.Event, *, host: str = "0.0.0.0", tools=None
) -> None:
    """Run the MCP endpoint until `stop` is set. Raises if the server fails or ends early."""
    app = build_app(tools or KickbaseTools(ctx), token)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False, lifespan="off"
    )
    server = _Server(config)
    task = asyncio.create_task(_serve(server, port))
    logger.info("Kickbase-Werkzeuge für Claude auf Port %d", port)
    stopped = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        stopped.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    if isinstance(result, Exception):
        raise result
    if not stop.is_set():
        raise RuntimeError("MCP-Server hat sich unerwartet beendet")
