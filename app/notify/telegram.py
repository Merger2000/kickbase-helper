"""Sends messages through the Telegram Bot API. The bot token is part of every request URL, so
it must never appear in logs or error messages: errors only carry status codes and types."""

import contextlib
import html
import logging

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_LENGTH = 4096  # Telegram's limit per message


class TelegramError(Exception):
    pass


class TelegramNotifier:
    def __init__(
        self, token: str, chat_id: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.chat_id = chat_id
        self._http = httpx.AsyncClient(
            base_url=f"{API_URL}/bot{token}", timeout=httpx.Timeout(15.0), transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(self, text: str) -> None:
        await self._send(text, html=True)

    async def send_plain(self, text: str) -> None:
        """Send without HTML parsing (for AI answers that may contain `<` or `&`)."""
        await self._send(text, html=False)

    async def send_markdown(self, text: str) -> None:
        """Send a message, rendering **bold** and *italic* as Telegram HTML. The text is
        escaped first, so raw `<`/`&` never break the parse mode."""
        from app.web.formatting import emphasis  # local: keeps the notifier free of web imports

        await self._send(emphasis(html.escape(text), "b", "i"), html=True)

    async def _send(self, text: str, *, html: bool) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:MAX_LENGTH],
            "disable_web_page_preview": True,
        }
        if html:
            payload["parse_mode"] = "HTML"
        try:
            response = await self._http.post("/sendMessage", json=payload)
        except httpx.HTTPError as exc:
            # str(exc) may contain the request URL and thus the token.
            raise TelegramError(f"Telegram nicht erreichbar ({type(exc).__name__})") from None
        if response.status_code != 200:
            try:
                body = response.json()
            except ValueError:
                body = None
            description = str(body.get("description", ""))[:200] if isinstance(body, dict) else ""
            status = response.status_code
            raise TelegramError(f"Telegram lehnt die Nachricht ab: HTTP {status} {description}")
        logger.info("Telegram-Nachricht gesendet (%d Zeichen)", len(payload["text"]))

    async def send_typing(self) -> None:
        """Show "tippt …" in the chat (lasts about 5 s). Failures are ignored."""
        payload = {"chat_id": self.chat_id, "action": "typing"}
        with contextlib.suppress(httpx.HTTPError):
            await self._http.post("/sendChatAction", json=payload)

    async def get_updates(self, offset: int | None = None, poll_timeout: int = 25) -> list[dict]:
        """Long-poll the bot for new updates. Returns the raw update objects (may be empty)."""
        payload = {"timeout": poll_timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            # Long-polling waits longer than the default request timeout.
            response = await self._http.post(
                "/getUpdates", json=payload, timeout=httpx.Timeout(40.0)
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"Telegram nicht erreichbar ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise TelegramError(f"Telegram lehnt getUpdates ab: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise TelegramError("Telegram getUpdates: Antwort ist kein JSON") from None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates: unerwartetes Format")
        return [update for update in result if isinstance(update, dict)]


TOKEN_SECRET = "telegram_bot_token"


def build_notifier(settings) -> TelegramNotifier | None:
    """None (notifications off) unless both the chat id and the token secret are configured."""
    from app.secret_files import SecretStatus, read_secret, secret_status

    if not settings.telegram_chat_id:
        logger.info("Telegram-Benachrichtigungen aus: TELEGRAM_CHAT_ID fehlt in .env")
        return None
    status = secret_status(settings.secrets_dir, TOKEN_SECRET)
    if status is not SecretStatus.OK:
        logger.warning("Telegram-Benachrichtigungen aus: Secret %s ist %s", TOKEN_SECRET, status)
        return None
    return TelegramNotifier(
        read_secret(settings.secrets_dir, TOKEN_SECRET), settings.telegram_chat_id
    )
