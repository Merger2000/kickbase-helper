#!/usr/bin/env python3
"""Kickbase-Helper Claude bridge: lets the worker container ask the Claude Code CLI on the host.

The CLI (`claude -p`) runs on the VM as the user that is logged in to Claude Code, so questions
are paid by the Claude subscription. The worker reaches the bridge over the Docker gateway.

* Standard library only; started by systemd (`kickbase-claude-bridge.service`).
* Binds to the Docker bridge gateway (default 172.17.0.1): reachable from containers, not the LAN.
* Bearer-token auth (constant-time compare). Prompts, answers and tokens are never logged.
* Each call may use exactly three things: WebSearch, WebFetch and the read-only Kickbase tools of
  the worker (MCP server at KICKBASE_MCP_URL, same token). No shell, no file access, no user
  settings, no other MCP servers. Conversations continue via `--resume <session_id>`.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 200_000
MAX_SYSTEM = 20_000
MAX_BUDGET_CAP = 5.0
MIN_TOKEN = 24
MIN_TIMEOUT = 30
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-\[\]]{0,63}")
TOOLS = "WebSearch,WebFetch"
ALLOWED_TOOLS = "WebSearch,WebFetch,mcp__kickbase"
# Variables of a surrounding Claude Code session (e.g. when testing from inside one) would make
# the CLI attach to that session instead of starting its own.
DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT")
NO_SESSION = "No conversation found"


class Config:
    def __init__(self, env: dict[str, str]) -> None:
        self.token = _token(env)
        self.host = env.get("BRIDGE_HOST", "172.17.0.1")
        self.port = int(env.get("BRIDGE_PORT", "8767"))
        self.claude_bin = (
            env.get("CLAUDE_BIN")
            or shutil.which("claude")
            or os.path.expanduser("~/.local/bin/claude")
        )
        # Upper bound; each request asks for its own timeout (config/ai.yaml) up to this.
        self.timeout = int(env.get("BRIDGE_TIMEOUT_S", "600"))
        self.concurrency = int(env.get("BRIDGE_MAX_CONCURRENCY", "1"))
        self.state_dir = Path(
            env.get("BRIDGE_STATE_DIR")
            or os.path.expanduser("~/.local/state/kickbase-claude-bridge")
        )
        self.mcp_url = env.get("KICKBASE_MCP_URL", "http://127.0.0.1:8768/mcp")
        self.default_model = env.get("BRIDGE_DEFAULT_MODEL", "sonnet")
        self.default_budget = min(float(env.get("BRIDGE_MAX_BUDGET_USD", "2.0")), MAX_BUDGET_CAP)

    @property
    def workdir(self) -> Path:
        return self.state_dir / "work"

    @property
    def mcp_config(self) -> Path:
        return self.state_dir / "mcp.json"


def _token(env: dict[str, str]) -> str:
    path = env.get("CLAUDE_BRIDGE_TOKEN_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return env.get("CLAUDE_BRIDGE_TOKEN", "").strip()


def log(**kw) -> None:
    kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(json.dumps(kw), file=sys.stderr, flush=True)


def write_mcp_config(cfg: Config) -> None:
    """The only MCP server Claude gets: the worker's Kickbase tools (0600, contains the token)."""
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.state_dir, 0o700)
    servers = {
        "kickbase": {
            "type": "http",
            "url": cfg.mcp_url,
            "headers": {"Authorization": f"Bearer {cfg.token}"},
        }
    }
    fd = os.open(cfg.mcp_config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": servers}, fh)
    os.chmod(cfg.mcp_config, 0o600)


class BadRequest(ValueError):
    pass


def parse_request(body: bytes, cfg: Config) -> dict:
    """Validate the JSON body of POST /v1/chat. Raises BadRequest with a short reason."""
    try:
        req = json.loads(body)
    except ValueError:
        raise BadRequest("kein JSON") from None
    if not isinstance(req, dict):
        raise BadRequest("kein Objekt")
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("prompt fehlt")
    model = req.get("model") or cfg.default_model
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        raise BadRequest("ungültiges Modell")
    session_id = req.get("session_id")
    if session_id is not None:
        try:
            session_id = str(uuid.UUID(str(session_id)))
        except ValueError:
            raise BadRequest("ungültige session_id") from None
    system = req.get("system")
    if system is not None and not isinstance(system, str):
        raise BadRequest("ungültiges system")
    budget = req.get("max_budget_usd", cfg.default_budget)
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or (isinstance(budget, float) and not math.isfinite(budget))
        or budget <= 0
    ):
        raise BadRequest("ungültiges Budget")
    timeout = req.get("timeout_s", 240)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or (isinstance(timeout, float) and not math.isfinite(timeout))
        or timeout <= 0
    ):
        raise BadRequest("ungültiger Timeout")
    return {
        "prompt": prompt,
        "model": model,
        "session_id": session_id,
        "system": (system or "")[:MAX_SYSTEM],
        "max_budget_usd": float(min(budget, MAX_BUDGET_CAP)),
        "timeout_s": int(min(max(timeout, MIN_TIMEOUT), cfg.timeout)),
    }


def build_command(cfg: Config, req: dict, *, resume: bool = True) -> list[str]:
    cmd = [
        cfg.claude_bin,
        "-p",
        "--model",
        req["model"],
        "--output-format",
        "json",
        "--tools",
        TOOLS,
        "--allowedTools",
        ALLOWED_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(cfg.mcp_config),
        "--max-budget-usd",
        f"{req['max_budget_usd']:.2f}",
    ]
    if req["system"]:
        cmd += ["--system-prompt", req["system"]]
    if resume and req["session_id"]:
        cmd += ["--resume", req["session_id"]]
    return cmd


def clean_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in DROP_ENV}


def parse_output(stdout: str, model: str) -> dict:
    """The CLI's JSON result → the bridge response (without prompt or answer in any log)."""
    out = json.loads(stdout)
    if not isinstance(out, dict):
        raise ValueError("unerwartete CLI-Ausgabe")
    usage = out.get("usage") or {}
    text = out.get("result")
    subtype = out.get("subtype") or ""
    ok = not out.get("is_error") and isinstance(text, str) and bool(text.strip())
    return {
        "ok": ok,
        "text": text if isinstance(text, str) else "",
        "error": None if ok else (subtype or "leere Antwort"),
        "session_id": out.get("session_id"),
        "model": next(iter((out.get("modelUsage") or {}).keys()), model),
        "cost_usd": out.get("total_cost_usd"),
        "num_turns": out.get("num_turns"),
        "output_tokens": usage.get("output_tokens"),
        "permission_denials": len(out.get("permission_denials") or []),
    }


def run_chat(cfg: Config, req: dict, runner=subprocess.run) -> tuple[int, dict]:
    """Run the CLI once (resuming the session if given). An unknown/expired session id starts a
    fresh conversation instead of failing."""
    resumed = bool(req["session_id"])
    proc = _run(cfg, req, runner, resume=resumed)
    if resumed and proc.returncode != 0 and NO_SESSION in (proc.stdout + proc.stderr):
        log(event="session_missing")
        resumed = False
        proc = _run(cfg, req, runner, resume=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        log(event="cli_error", code=proc.returncode)
        return 502, {"ok": False, "error": f"claude CLI exited {proc.returncode}"}
    resp = parse_output(proc.stdout, req["model"])
    resp["resumed"] = resumed
    return (200 if resp["ok"] else 502), resp


def _run(cfg: Config, req: dict, runner, *, resume: bool):
    return runner(
        build_command(cfg, req, resume=resume),
        input=req["prompt"],
        capture_output=True,
        text=True,
        timeout=req["timeout_s"],
        cwd=cfg.workdir,
        env=clean_env(dict(os.environ)),
    )


def make_handler(cfg: Config, sem: threading.Semaphore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KickbaseClaudeBridge/1.0"

        def log_message(self, *_):  # keep the default access log quiet
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log(event="client_disconnected", status=code)

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {cfg.token}"
            return bool(cfg.token) and hmac.compare_digest(auth.encode(), expected.encode())

        def do_GET(self):
            # /health needs no token: it only says whether the bridge and the CLI are there.
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            available = bool(cfg.claude_bin and os.path.exists(cfg.claude_bin))
            return self._send(200, {"ok": True, "available": available})

        def do_POST(self):
            if self.path != "/v1/chat":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"error": "bad body size"})
            try:
                req = parse_request(self.rfile.read(length), cfg)
            except BadRequest as exc:
                return self._send(400, {"error": str(exc)})
            if not sem.acquire(timeout=30):
                return self._send(429, {"error": "bridge busy"})
            started = time.time()
            try:
                code, resp = run_chat(cfg, req)
                log(
                    event="chat",
                    model=resp.get("model", req["model"]),
                    ok=resp.get("ok"),
                    resumed=resp.get("resumed"),
                    turns=resp.get("num_turns"),
                    cost_usd=resp.get("cost_usd"),
                    latency_s=round(time.time() - started, 1),
                )
                return self._send(code, resp)
            except subprocess.TimeoutExpired:
                log(event="timeout", model=req["model"])
                return self._send(504, {"ok": False, "error": "timeout"})
            except (ValueError, OSError) as exc:
                log(event="bridge_error", error=type(exc).__name__)
                return self._send(502, {"ok": False, "error": "bridge failure"})
            finally:
                sem.release()

    return Handler


def main() -> None:
    cfg = Config(dict(os.environ))
    if not cfg.token:
        # Empty token file = feature switched off; exit 0 so systemd does not restart us.
        print("Claude-Bridge aus: Token-Datei leer oder nicht vorhanden", file=sys.stderr)
        sys.exit(0)
    if len(cfg.token) < MIN_TOKEN:
        print(f"Token zu kurz (CLAUDE_BRIDGE_TOKEN_FILE, >= {MIN_TOKEN} Zeichen)", file=sys.stderr)
        sys.exit(2)
    if not cfg.claude_bin or not os.path.exists(cfg.claude_bin):
        print("claude CLI nicht gefunden; CLAUDE_BIN setzen", file=sys.stderr)
        sys.exit(2)
    write_mcp_config(cfg)
    handler = make_handler(cfg, threading.Semaphore(max(1, cfg.concurrency)))
    srv = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    log(event="listening", host=cfg.host, port=cfg.port, mcp=cfg.mcp_url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
