"""Sends a test message: `docker compose exec worker python -m app.notify.check`."""

import asyncio
import sys

from app.config import get_settings
from app.log import configure_logging
from app.notify.telegram import TelegramError, build_notifier


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    notifier = build_notifier(settings)
    if notifier is None:
        print(
            "Telegram ist nicht konfiguriert (TELEGRAM_CHAT_ID oder Secret fehlt).", file=sys.stderr
        )
        return 1
    try:
        await notifier.send(
            f"<b>{settings.kickbase_league_name}</b>\n"
            "✅ Kickbase Helper ist verbunden. Hier kommen Meldungen zu starken Marktwert-"
            "Sprüngen und Statusänderungen deiner Spieler."
        )
    except TelegramError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    finally:
        await notifier.aclose()
    print("Testnachricht gesendet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
