"""Client for the Claude bridge on the VM (`deploy/claude-bridge/bridge.py`).

The bridge runs the Claude Code CLI with web search and the worker's read-only Kickbase tools.
Errors never contain the token or the URL.
"""

import logging
from dataclasses import dataclass

import httpx

from app.secret_files import SecretStatus, read_secret, secret_status

logger = logging.getLogger(__name__)

TOKEN_SECRET = "claude_bridge_token"
EXTRA_TIMEOUT = 30.0  # on top of the CLI timeout of the bridge
MIN_TOKEN = 24  # same minimum as the bridge


class ClaudeBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeReply:
    text: str
    session_id: str | None
    model: str
    cost_usd: float | None


class ClaudeBridge:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 240.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token  # also guards the MCP endpoint of the worker
        self.timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout + EXTRA_TIMEOUT, connect=5.0),
            transport=transport,
        )

    async def chat(
        self,
        prompt: str,
        *,
        model: str,
        session_id: str | None = None,
        system: str | None = None,
        max_budget_usd: float | None = None,
    ) -> ClaudeReply:
        body: dict = {"prompt": prompt, "model": model, "timeout_s": self.timeout}
        if session_id:
            body["session_id"] = session_id
        if system:
            body["system"] = system
        if max_budget_usd:
            body["max_budget_usd"] = max_budget_usd
        try:
            resp = await self._http.post("/v1/chat", json=body)
        except httpx.TimeoutException:
            raise ClaudeBridgeError("Zeitüberschreitung") from None
        except httpx.HTTPError as exc:
            raise ClaudeBridgeError(f"Bridge nicht erreichbar ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            reason = data.get("error") or f"HTTP {resp.status_code}"
            raise ClaudeBridgeError(f"Claude-Fehler: {str(reason)[:120]}")
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ClaudeBridgeError("leere Antwort")
        session = data.get("session_id")
        cost = data.get("cost_usd")
        return ClaudeReply(
            text=text.strip(),
            session_id=session if isinstance(session, str) else None,
            model=str(data.get("model") or model),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def build_claude_bridge(settings, timeout: float = 240.0) -> ClaudeBridge | None:
    status = secret_status(settings.secrets_dir, TOKEN_SECRET)
    if status is not SecretStatus.OK or not settings.claude_bridge_url:
        logger.info("Claude im Chat aus: Secret %s ist %s", TOKEN_SECRET, status)
        return None
    token = read_secret(settings.secrets_dir, TOKEN_SECRET)
    if len(token) < MIN_TOKEN:
        logger.warning(
            "Claude im Chat aus: Secret %s ist kürzer als %d Zeichen", TOKEN_SECRET, MIN_TOKEN
        )
        return None
    logger.info("Claude im Chat an (Bridge %s)", settings.claude_bridge_url)
    return ClaudeBridge(settings.claude_bridge_url, token, timeout=timeout)
