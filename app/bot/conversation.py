"""Answer a Telegram question from our own data via the language model.

Every question gets the "Lagebild" (app/bot/context.py) – deadline, budget, lineup, squad with
scores, market – so most questions need a single call. For anything else (another player, a
filtered market search) the model can call a read-only tool; the result is fed back and the loop
repeats, up to `MAX_TOOL_ROUNDS` rounds. The last few questions and answers are passed along so
follow-ups ("und der?") work.
"""

import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from app.bot.context import build_situation
from app.bot.tools import ALIASES, TOOLS, ToolContext, _parse_market_query, named_players, run_tool
from app.kickbase.errors import KickbaseError
from app.llm.client import LlmClient
from app.services.briefing import unknown_numbers

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 2
# Hard cap per question: tool rounds + one repair round + one answer. Never more model calls.
MAX_CALLS = MAX_TOOL_ROUNDS + 2
UNVERIFIED = "⚠️ Ohne Gewähr – diese Zahlen stehen nicht in deinen Daten: {}"
TOOL_LINE = re.compile(r"WERKZEUG:\s*(\w+)\s*\(([^)\n]*)\)")
NO_MORE_TOOLS = "Antworte jetzt ohne weiteres Werkzeug mit den vorhandenen Daten."
# Words that suggest a market question ("Welcher Stürmer lohnt sich?").
BUY_WORDS = re.compile(r"kauf|markt|hol|lohnt|günstig|angebot|bieten|gebot|unter \d", re.I)

KICKBASE_RULES = """Kickbase ist ein Bundesliga-Managerspiel: Man kauft und verkauft Spieler auf
einem Transfermarkt (per Gebot), stellt vor jedem Spieltag eine Elf auf und bekommt die Punkte
der aufgestellten Spieler.

Regeln von Kickbase:
- Bis zur Aufstellungs-Deadline muss die Elf stehen; leere Plätze bringen 0 Punkte.
- Ist der Kontostand negativ (im Minus), gibt es am Spieltag KEINE Punkte. Dann vor der Deadline
  verkaufen, bis das Konto mindestens 0 ist – empfiehl dafür, falls vorhanden, die EMPFEHLUNG
  aus „AUS DEM MINUS“ (geringster Punkteverlust), nicht einfach den teuersten Spieler.
- Gewinnt ein Gebot, wird der Preis abgebucht; Gebote können ins Minus führen.
- Marktwerte ändern sich einmal täglich (gegen 22 Uhr).
- Punkte gibt es nur, wenn der Spieler spielt: Startchance und Status zählen.

Score: 0–100 aus Form (letzte Spieltage), Marktwert-Trend, Programm (Gegner), Einsatz
(Startchance), Klasse (Marktwert-Rang und Saison-Ø) und Status – höher ist besser."""

SYSTEM = (
    "Du bist der Kickbase-Assistent des Nutzers im Telegram-Chat. "
    + KICKBASE_RULES
    + """

Du bekommst das LAGEBILD des Nutzers (Stand jetzt) und ggf. DATEN zu Spielern oder zum Markt.

So antwortest du:
- Deutsch, wie ein guter Berater: zuerst die klare Antwort oder Empfehlung in einem Satz, dann
  1–3 kurze Gründe mit Zahlen als Stichpunkte "- ". Höchstens 80 Wörter. Fett **…** nur für
  einzelne Namen, nie verschachtelt.
- Beantworte genau die gestellte Frage. Werte eines Spielers (Score, Startchance …) nur aus
  seiner eigenen Zeile bzw. seinen DATEN, nie von einem anderen Spieler übernehmen.
- Fakten und Zahlen NUR aus Lagebild, DATEN, Chat oder Frage – exakt übernehmen, nicht runden,
  nicht selbst rechnen. Kein Allgemeinwissen über Spieler oder Vereine (es ist oft veraltet).
- Fehlt etwas, sag es ehrlich.
- Dringendes (Minus vor der Deadline, leere Plätze, Ausfälle in der Aufstellung) nennst du
  ungefragt höchstens in einem kurzen Schlusssatz – und nicht, wenn du es im BISHERIGEN CHAT
  schon gesagt hast.
- Brauchst du Daten zu einem Spieler oder eine Marktsuche, die noch fehlen, antworte NUR mit
  einer Zeile: WERKZEUG: spieler(Name)  oder  WERKZEUG: markt(Position bis Preis in Mio.)"""
)


def _prompt(
    situation: str,
    question: str,
    history: list[tuple[str, str]],
    data: list[tuple[str, str | None, str]],
    retry_note: str | None = None,
) -> str:
    parts = [f"LAGEBILD:\n{situation}"]
    for tool, argument, result in data:
        parts.append(f"DATEN {tool}({argument or ''}):\n{result}")
    if history:
        chat = "\n".join(f"Nutzer: {q}\nDu: {a}" for q, a in history)
        parts.append(f"BISHERIGER CHAT (für Rückfragen):\n{chat}")
    parts.append(f"FRAGE: {question}")
    if retry_note:
        parts.append(retry_note)
    return "\n\n".join(parts)


async def _prefetch(ctx: ToolContext, question: str) -> list[tuple[str, str | None, str]]:
    """Load what the question obviously needs, so the model rarely has to pick a tool."""
    data = []
    for name in await named_players(ctx.sessions, question):
        data.append(("spieler", name, await run_tool(ctx, "spieler", name)))
    position, max_price, _ = _parse_market_query(question)
    if BUY_WORDS.search(question) and (position or max_price):
        data.append(("markt", question, await run_tool(ctx, "markt", question)))
    return data


async def answer(
    llm: LlmClient,
    ctx: ToolContext,
    question: str,
    history: list[tuple[str, str]] | None = None,
    tz: ZoneInfo | None = None,
) -> str:
    """Answer one question. Raises LlmError when the model can't be asked at all."""
    history = history or []
    try:
        situation = await build_situation(ctx, tz or ZoneInfo("Europe/Berlin"))
        ctx.situation = situation
        situation_text = situation.text
    except (KickbaseError, SQLAlchemyError) as exc:
        logger.warning("Lagebild nicht verfügbar: %s: %s", type(exc).__name__, exc)
        situation_text = "Lagebild gerade nicht verfügbar (Kickbase oder Datenbank gestört)."
    except Exception:  # a bug here must not cost the answer: the tools still work
        logger.exception("Lagebild fehlgeschlagen")
        situation_text = "Lagebild gerade nicht verfügbar (interner Fehler)."
    try:
        data = await _prefetch(ctx, question)
    except Exception:  # the prefetch is a convenience, never a reason to fail
        logger.exception("Vorab-Daten fehlgeschlagen")
        data = []

    note: str | None = None
    tool_rounds, repaired = 0, False
    for _ in range(MAX_CALLS):
        prompt = _prompt(situation_text, question, history, data, note)
        reply = (await llm.complete_text("chat", SYSTEM, prompt)).data
        call = TOOL_LINE.search(reply)
        text = _without_tool_lines(reply)
        if call is not None and not text:
            name = ALIASES.get(call.group(1).lower(), call.group(1).lower())
            if name in TOOLS and tool_rounds < MAX_TOOL_ROUNDS:
                argument = call.group(2).strip().strip("\"'")
                data.append((name, argument, await run_tool(ctx, name, argument)))
                tool_rounds += 1
                continue
            logger.warning("KI-Werkzeug abgelehnt: %s", call.group(0))
            if note == NO_MORE_TOOLS:
                break  # asked twice for a tool it can't have: stop, don't burn calls
            note = NO_MORE_TOOLS
            continue
        if call is not None and tool_rounds < MAX_TOOL_ROUNDS:
            # "Ich schaue nach.\nWERKZEUG: …": the tool is what the model really wants.
            name = ALIASES.get(call.group(1).lower(), call.group(1).lower())
            if name in TOOLS:
                argument = call.group(2).strip().strip("\"'")
                data.append((name, argument, await run_tool(ctx, name, argument)))
                tool_rounds += 1
                continue
        # Every number must come from our data or the user's own words – not from earlier
        # answers, which may carry an unverified number.
        source = "\n".join(
            [situation_text, question, *(r for _, _, r in data), *(q for q, _ in history)]
        )
        unknown = unknown_numbers(text, source)
        if not unknown:
            return text
        logger.warning("Bot-Antwort mit unbekannten Zahlen: %s", unknown[:5])
        if repaired:
            # Second try still has them: send it, but say which numbers are unverified.
            return f"{text}\n\n{UNVERIFIED.format(', '.join(unknown[:5]))}"
        repaired = True
        note = (
            f"Deine letzte Antwort enthielt Zahlen, die nicht in den Daten stehen: "
            f"{', '.join(unknown[:5])}. Antworte neu und übernimm Zahlen nur exakt aus "
            "Lagebild oder DATEN (nicht selbst rechnen)."
        )
    return "Die Frage war in der erlaubten Zahl von Schritten nicht zu beantworten."


def _without_tool_lines(reply: str) -> str:
    """The answer without tool requests (noise for the user)."""
    lines = [line for line in reply.splitlines() if "WERKZEUG:" not in line]
    return "\n".join(lines).strip()
