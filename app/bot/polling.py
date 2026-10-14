"""Telegram long-polling: answer questions from the own chat with our data."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from app.bot import claude_mode
from app.bot.conversation import answer
from app.bot.tools import ToolContext
from app.llm.client import LlmClient
from app.llm.config import load_ai_config
from app.llm.errors import LlmError
from app.notify.telegram import TelegramError
from app.worker.jobs import JobContext

logger = logging.getLogger("app.bot")

QUESTIONS_KEY = "bot.questions"
HISTORY_KEY = "bot.history"
# Follow-ups ("und der?") refer to the last few questions of the same conversation.
HISTORY_TURNS = 3
HISTORY_MAX_AGE = timedelta(minutes=30)
POLL_ERROR_SLEEP = 15.0


async def poll_questions(ctx: JobContext, stop: asyncio.Event) -> None:
    """Long-poll the bot for messages from the own chat id and answer them. Runs until `stop`."""
    notifier, llm = ctx.notifier, ctx.llm
    if notifier is None or (llm is None and ctx.claude is None):
        logger.info("Fragen aus: Telegram oder KI nicht konfiguriert")
        return
    limit = load_ai_config().limits.questions_per_day
    offset: int | None = None
    while not stop.is_set():
        try:
            offset = await _poll_once(ctx, llm, limit, offset)
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            logger.warning("Telegram getUpdates fehlgeschlagen: %s", exc)
            await _sleep(stop, POLL_ERROR_SLEEP)
        except Exception:
            # Whatever goes wrong, the bot keeps polling (and the error is in the log).
            logger.exception("Fehler beim Abholen der Telegram-Fragen")
            await _sleep(stop, POLL_ERROR_SLEEP)


async def _poll_once(
    ctx: JobContext, llm: LlmClient | None, limit: int, offset: int | None
) -> int | None:
    """One getUpdates round; returns the next offset."""
    notifier = ctx.notifier
    updates = await notifier.get_updates(offset=offset)
    for update in updates:
        if isinstance(update.get("update_id"), int):
            offset = update["update_id"] + 1
        try:
            await _answer_update(ctx, llm, limit, update)
        except Exception:
            # Never let one update stop the batch: the offset already moved past it, so a
            # failure here can't make the next poll answer the same questions again.
            logger.exception("Telegram-Update nicht verarbeitet")
    return offset


async def _answer_update(ctx: JobContext, llm: LlmClient | None, limit: int, update: dict) -> None:
    """Answer one update if it is a text message from the own chat."""
    notifier = ctx.notifier
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    if not isinstance(chat, dict) or str(chat.get("id")) != notifier.chat_id:
        return  # only the own chat
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    question: str | None = text.strip()
    try:
        # `@claude …`, `/claude_model` and every message inside an open Claude window.
        handled = await claude_mode.maybe_handle(ctx, question)
    except Exception:
        logger.exception("Claude-Nachricht nicht verarbeitet")
        handled = claude_mode.Handled(["Claude hat gerade nicht geklappt."], question)
    if handled is not None:
        for part in handled.replies:
            await _send(notifier, part)
        question = handled.fallback_question
    if not question:
        return
    if llm is None:
        await _send(notifier, "Die normale KI ist nicht eingerichtet – frag mit @claude …")
        return
    try:
        reply = await _handle(ctx, llm, question, limit)
    except Exception:  # a bad question must not kill the polling task
        logger.exception("Frage nicht verarbeitet")
        reply = "Das hat leider nicht geklappt."
    if reply:
        await _send(notifier, reply)


async def _send(notifier, reply: str) -> None:
    try:
        await notifier.send_markdown(reply)
    except TelegramError as exc:
        # Invalid HTML (e.g. unbalanced `**`) gets a 400: retry as plain text.
        logger.warning("Telegram send (HTML) fehlgeschlagen: %s", exc)
        try:
            await notifier.send_plain(reply)
        except TelegramError as exc2:
            logger.warning("Telegram send fehlgeschlagen: %s", exc2)


async def _handle(ctx: JobContext, llm: LlmClient, text: str, limit: int) -> str | None:
    today = datetime.now(UTC).astimezone(ctx.tz).date().isoformat()
    key = f"{QUESTIONS_KEY}.{today}"
    used = await ctx.kv.get(key)
    if not isinstance(used, int):
        used = 0
    if used >= limit:
        return "Das Fragen-Kontingent für heute ist aufgebraucht."
    league = await ctx.league.get()
    now = datetime.now(UTC)
    bot_ctx = ToolContext(ctx.client, ctx.sessions, league, ctx.oldb, now)
    history = await _recent_history(ctx, now)
    try:
        reply = await answer(llm, bot_ctx, text, [(h["q"], h["a"]) for h in history], ctx.tz)
    except LlmError as exc:
        logger.warning("Frage nicht beantwortbar: %s", exc)
        return f"KI gerade nicht verfügbar ({exc})."
    await ctx.kv.set(key, used + 1)
    # The chat history never keeps an unverified number (it would look verified next time).
    entry = {"at": now.isoformat(), "q": text, "a": reply.split("\n\n⚠️ Ohne Gewähr")[0]}
    await ctx.kv.set(HISTORY_KEY, [*history, entry][-HISTORY_TURNS:])
    return reply


async def _recent_history(ctx: JobContext, now: datetime) -> list[dict]:
    """The last questions and answers of this conversation (none after a longer pause)."""
    stored = await ctx.kv.get(HISTORY_KEY)
    if not isinstance(stored, list):
        return []
    recent = []
    for item in stored:
        try:
            at = datetime.fromisoformat(item["at"])
            if now - at <= HISTORY_MAX_AGE:
                recent.append({"at": item["at"], "q": str(item["q"]), "a": str(item["a"])})
        except (KeyError, TypeError, ValueError):
            continue  # a malformed entry only loses the context, never the answer
    return recent[-HISTORY_TURNS:]


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
