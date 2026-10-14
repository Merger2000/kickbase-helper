"""The host-side Claude bridge (`deploy/claude-bridge/bridge.py`, stdlib only)."""

import importlib.util
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "claude-bridge" / "bridge.py"
_spec = importlib.util.spec_from_file_location("claude_bridge_script", SCRIPT)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

SESSION = "0b6f1c1e-3f9e-4a39-9b0c-6a8f8f2f1a11"


@pytest.fixture
def cfg(tmp_path):
    return bridge.Config(
        {
            "CLAUDE_BRIDGE_TOKEN": "t" * 32,
            "CLAUDE_BIN": "/usr/bin/claude",
            "BRIDGE_STATE_DIR": str(tmp_path / "state"),
        }
    )


def body(**kw) -> bytes:
    return json.dumps(kw).encode()


def test_token_from_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("abc\n")
    assert bridge.Config({"CLAUDE_BRIDGE_TOKEN_FILE": str(token_file)}).token == "abc"
    assert bridge.Config({"CLAUDE_BRIDGE_TOKEN_FILE": str(tmp_path / "missing")}).token == ""


def test_parse_request_defaults_and_caps(cfg):
    req = bridge.parse_request(body(prompt="Hi", max_budget_usd=50), cfg)
    assert req["model"] == "sonnet"
    assert req["session_id"] is None
    assert req["max_budget_usd"] == bridge.MAX_BUDGET_CAP
    assert bridge.parse_request(body(prompt="Hi", session_id=SESSION), cfg)["session_id"] == SESSION


def test_parse_request_clamps_the_timeout(cfg):
    assert cfg.timeout == 600
    assert bridge.parse_request(body(prompt="Hi"), cfg)["timeout_s"] == 240
    assert bridge.parse_request(body(prompt="Hi", timeout_s=300), cfg)["timeout_s"] == 300
    assert bridge.parse_request(body(prompt="Hi", timeout_s=5), cfg)["timeout_s"] == 30
    assert bridge.parse_request(body(prompt="Hi", timeout_s=9999), cfg)["timeout_s"] == 600


def test_huge_integer_limits_are_capped_without_float_overflow(cfg):
    req = bridge.parse_request(body(prompt="Hi", timeout_s=10**1000, max_budget_usd=10**1000), cfg)
    assert req["timeout_s"] == 600
    assert req["max_budget_usd"] == bridge.MAX_BUDGET_CAP


@pytest.mark.parametrize(
    "payload",
    [
        b"nope",
        b"[1]",
        body(prompt=" "),
        body(prompt="x", model="opus; rm -rf /"),
        body(prompt="x", model="--dangerously"),
        body(prompt="x", session_id="../etc"),
        body(prompt="x", system=3),
        body(prompt="x", max_budget_usd=True),
        body(prompt="x", max_budget_usd=-1),
        body(prompt="x", timeout_s="60"),
        body(prompt="x", timeout_s=0),
        body(prompt="x", timeout_s=float("nan")),
        body(prompt="x", timeout_s=float("inf")),
        body(prompt="x", max_budget_usd=float("nan")),
        body(prompt="x", max_budget_usd=float("inf")),
    ],
)
def test_parse_request_rejects(cfg, payload):
    with pytest.raises(bridge.BadRequest):
        bridge.parse_request(payload, cfg)


def test_command_is_locked_down(cfg):
    req = bridge.parse_request(body(prompt="Hi", system="Sys", session_id=SESSION), cfg)
    cmd = bridge.build_command(cfg, req)
    assert cmd[0] == "/usr/bin/claude"
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch,mcp__kickbase"
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "Sys"
    assert cmd[cmd.index("--resume") + 1] == SESSION
    assert "Hi" not in cmd  # the prompt goes through stdin
    assert "--resume" not in bridge.build_command(cfg, req, resume=False)


def test_mcp_config_is_private(cfg):
    bridge.write_mcp_config(cfg)
    data = json.loads(cfg.mcp_config.read_text())
    assert data["mcpServers"]["kickbase"]["headers"]["Authorization"] == "Bearer " + "t" * 32
    assert cfg.mcp_config.stat().st_mode & 0o777 == 0o600


def test_clean_env_drops_outer_session():
    env = bridge.clean_env({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "HOME": "/h"})
    assert env == {"HOME": "/h"}


def cli_output(**kw) -> str:
    out = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Antwort",
        "session_id": SESSION,
        "total_cost_usd": 0.12,
        "num_turns": 3,
        "usage": {"output_tokens": 50},
        "modelUsage": {"claude-sonnet-5-5": {}},
        "permission_denials": [],
    }
    out.update(kw)
    return json.dumps(out)


def test_parse_output():
    resp = bridge.parse_output(cli_output(), "sonnet")
    assert resp["ok"] and resp["text"] == "Antwort"
    assert resp["model"] == "claude-sonnet-5-5"
    assert resp["session_id"] == SESSION
    failed = bridge.parse_output(cli_output(is_error=True, subtype="error_max_budget_usd"), "x")
    assert not failed["ok"] and failed["error"] == "error_max_budget_usd"


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.commands: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.commands.append(cmd)
        assert kw["input"] == "Hi"
        rc, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(cmd, rc, out, err)


def test_cli_failure_never_logs_stderr(cfg, capsys):
    req = bridge.parse_request(body(prompt="Hi"), cfg)
    runner = Runner((1, "", "private-prompt bearer-test-secret"))
    code, response = bridge.run_chat(cfg, req, runner)
    assert code == 502
    assert response["error"] == "claude CLI exited 1"
    logs = capsys.readouterr()
    assert "private-prompt" not in logs.out + logs.err
    assert "bearer-test-secret" not in logs.out + logs.err


def test_run_chat_uses_the_request_timeout(cfg):
    seen: list[float] = []

    def runner(cmd, **kw):
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, cli_output(), "")

    req = bridge.parse_request(body(prompt="Hi", timeout_s=90), cfg)
    assert bridge.run_chat(cfg, req, runner)[0] == 200
    assert seen == [90]


def test_run_chat_resumes(cfg):
    req = bridge.parse_request(body(prompt="Hi", session_id=SESSION), cfg)
    runner = Runner((0, cli_output(), ""))
    code, resp = bridge.run_chat(cfg, req, runner)
    assert code == 200 and resp["resumed"] is True
    assert "--resume" in runner.commands[0]


def test_run_chat_starts_fresh_when_the_session_is_gone(cfg):
    req = bridge.parse_request(body(prompt="Hi", session_id=SESSION), cfg)
    runner = Runner(
        (1, "", f"No conversation found with session ID: {SESSION}"), (0, cli_output(), "")
    )
    code, resp = bridge.run_chat(cfg, req, runner)
    assert code == 200 and resp["resumed"] is False
    assert "--resume" not in runner.commands[1]


def test_run_chat_cli_failure(cfg):
    req = bridge.parse_request(body(prompt="Hi"), cfg)
    code, resp = bridge.run_chat(cfg, req, Runner((2, "", "boom")))
    assert code == 502 and resp["ok"] is False


@pytest.fixture
def server(cfg, monkeypatch):
    """The real HTTP handler on a free local port; `run_chat` is replaced per test."""
    outcome: dict = {"result": (200, {"ok": True, "text": "Antwort"})}

    def fake_run_chat(cfg_, req):
        result = outcome["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(bridge, "run_chat", fake_run_chat)
    httpd = bridge.ThreadingHTTPServer(
        ("127.0.0.1", 0), bridge.make_handler(cfg, threading.Semaphore(1))
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", outcome
    httpd.shutdown()
    httpd.server_close()


def call(url, path, data=None, token="t" * 32, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_health_without_token(server):
    url, _ = server
    status, data = call(url, "/health", token=None)
    assert status == 200 and data["ok"] is True and "available" in data
    assert call(url, "/nope")[0] == 404


def test_http_chat(server):
    url, _ = server
    assert call(url, "/v1/chat", body(prompt="Hi")) == (200, {"ok": True, "text": "Antwort"})
    assert call(url, "/v1/other", body(prompt="Hi"))[0] == 404


def test_http_rejects_bad_token(server):
    url, _ = server
    assert call(url, "/v1/chat", body(prompt="Hi"), token=None)[0] == 401
    assert call(url, "/v1/chat", body(prompt="Hi"), token="falsch")[0] == 401


def test_http_bad_bodies(server):
    url, _ = server
    assert call(url, "/v1/chat", b"", method="POST")[0] == 413
    assert call(url, "/v1/chat", b"nope")[0] == 400


@pytest.mark.parametrize(
    ("error", "status"),
    [(subprocess.TimeoutExpired("claude", 1), 504), (OSError("kaputt"), 502)],
)
def test_http_cli_failures(server, error, status):
    url, outcome = server
    outcome["result"] = error
    code, data = call(url, "/v1/chat", body(prompt="Hi"))
    assert code == status and data["ok"] is False


def test_main_exits_cleanly_without_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("CLAUDE_BRIDGE_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        bridge.main()
    assert exc.value.code == 0
    (tmp_path / "short").write_text("kurz")
    monkeypatch.setenv("CLAUDE_BRIDGE_TOKEN_FILE", str(tmp_path / "short"))
    with pytest.raises(SystemExit) as exc:
        bridge.main()
    assert exc.value.code == 2
