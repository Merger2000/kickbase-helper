"""A short AI summary of a player for the detail page, generated on demand and cached per day.

Only the player's own public Bundesliga data and our scores/news go into the prompt (never other
managers). The model's answer is verified like the briefing: every number must occur in the input,
otherwise the summary is dropped. Without an LLM client (no key) or on any error the caller shows
a plain hint instead.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.db.stores import KeyValueStore
from app.kickbase.calendar import market_day
from app.llm.client import LlmClient
from app.news.service import ReportView
from app.services.briefing import unknown_numbers
from app.services.players import PlayerPage

if TYPE_CHECKING:
    from app.services.decision import Decision

logger = logging.getLogger(__name__)

# v2: the assessment (pro/contra) replaced the plain summary; old texts are not served again.
CACHE_PREFIX = "ai.player2"
MAX_NEWS = 5

SYSTEM = """Du erklärst einem Kickbase-Manager (Bundesliga-Managerspiel: Punkte gibt es nur für
Spieler, die spielen) die Einschätzung eines Spielers. Die Zahlen stehen schon auf der Seite –
du ordnest sie ein, statt sie aufzuzählen.

Antworte mit genau drei Zeilen:
**Dafür:** ein Satz – das stärkste Argument (aus den Score-Bestandteilen, Startchance, Gegnern).
**Dagegen:** ein Satz – das größte Risiko oder die größte Schwäche.
**Achte auf:** ein Satz – worauf es in den nächsten Tagen ankommt (Gegner, Status, Meldung,
Startelf, Marktwert).

Regeln:
- Nur die gegebenen Fakten, nichts aus deinem Allgemeinwissen (es ist oft veraltet). Keine
  Ranglisten, Vergleiche oder Statistiken, die nicht dastehen (z. B. "Top 5 %", Bilanzen).
- Höchstens zwei Zahlen pro Satz, exakt übernommen, nicht selbst gerechnet.
- Ohne Daten zu einem Punkt: sag das kurz, statt etwas zu erfinden."""


class PlayerSummary(BaseModel):
    text: str = Field(description="Drei Zeilen: Dafür, Dagegen, Achte auf")


def _mio(value: int) -> str:
    return f"{value / 1_000_000:.1f} Mio."


def build_prompt(
    page: PlayerPage, news: list[ReportView], decision: "Decision | None" = None
) -> str:
    lines = [
        f"Spieler: {page.name} ({page.position.short_label}, {page.team_name or '?'})",
        f"Status: {page.status_label}",
        f"Marktwert: {_mio(page.market_value)} €",
    ]
    if decision is not None:
        where = "im eigenen Kader" if decision.in_squad else "nicht im eigenen Kader"
        lines.append(
            f"Einschätzung des Dashboards ({where}): {decision.verdict}, Score "
            f"{decision.score.shown_total} von 100"
        )
        shown = decision.score.shown_points
        lines.append("Score-Bestandteile (Punkte, Begründung):")
        for c in decision.score.components:
            lines.append(f"- {c.label} {shown[c.key]:+d}: {c.reason}")
        lines += [f"- Grund: {r}" for r in decision.reasons]
    start = page.start_probability
    if start.percent is not None:
        lines.append(f"Startchance: {start.percent} % ({'; '.join(start.reasons)})")
    for match in page.upcoming[:3]:
        where = "zu Hause gegen" if match.is_home else "auswärts bei"
        lines.append(f"Nächstes Spiel: {match.matchday}. Spieltag {where} {match.opponent or '?'}")
    if page.market_value_change_7d_percent is not None:
        lines.append(f"Marktwert-Änderung in 7 Tagen: {page.market_value_change_7d_percent:+.1f} %")
    if page.market_value_change_24h is not None:
        lines.append(
            f"Marktwert-Änderung beim letzten Update (24 h): {page.market_value_change_24h:+d} €"
        )
        lines.append(
            "Für Trading zählt die aktuelle Bewegung; 7 Tage sind nur Kontext. "
            "Anstieg ist keine Gewinngarantie."
        )
    lines.append(
        f"Kickbase-Punkte diese Saison: {page.total_points} gesamt in {page.matches_played} "
        f"Einsätzen, Ø {page.average_points} pro Einsatz, {page.goals} Tore, {page.assists} "
        "Vorlagen"
    )
    if page.start_quote is not None:
        lines.append(
            f"Startelf: {page.start_quote.started} von {page.start_quote.total} "
            "der letzten Vereinsspiele seit seinem ersten Einsatz für den Verein von Beginn an "
            "(nicht im Kader zählt als nicht gestartet)"
        )
    if page.predicted_starting:
        lines.append("LigaInsider: in der voraussichtlichen Startelf")
    if news:
        lines.append("Meldungen:")
        for r in news[:MAX_NEWS]:
            detail = f" ({r.absence})" if r.absence else ""
            lines.append(f"- {r.kind_label}: {r.status_label}{detail}")
    return "\n".join(lines)


def _three_lines(text: str) -> str:
    """One line per point, however the model joined them (the page shows them as lines)."""
    for label in ("**Dagegen:**", "**Achte auf:**"):
        text = text.replace(f" {label}", f"\n{label}")
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


async def summarize(
    llm: LlmClient,
    kv: KeyValueStore,
    page: PlayerPage,
    news: list[ReportView],
    now: datetime,
    decision: "Decision | None" = None,
) -> str | None:
    """The summary text, or None when the model produced nothing usable (empty or numbers that
    don't come from the input). Raises LlmError / SQLAlchemyError on transport or DB problems."""
    # Own player or not, with or without the verdict: a different text, so a different key.
    variant = "x" if decision is None else ("s" if decision.in_squad else "k")
    key = f"{CACHE_PREFIX}.{page.id}.{market_day(now)}.{variant}"
    cached = await kv.get(key)
    if cached:
        return cached
    prompt = build_prompt(page, news, decision)
    completion = await llm.complete("player", SYSTEM, prompt, "spieler", PlayerSummary)
    text = _three_lines(completion.data.text)
    if not text:
        logger.warning("KI lieferte leeren Text für Spieler %s", page.id)
        return None
    if unknown_numbers(text.replace(",", "."), prompt.replace(",", ".")):
        logger.warning("KI-Text für Spieler %s verworfen (unbekannte Zahlen)", page.id)
        return None
    await kv.set(key, text)
    return text
