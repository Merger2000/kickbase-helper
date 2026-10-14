"""Daily briefing: a few lines on what to do today, written by the language model from our own
numbers only.

The model gets a compact JSON of the dashboard data (squad, budget, buy and sell candidates with
their score components, news about own players) and must reuse names and numbers exactly. Every
number in its text has to appear in that JSON; otherwise – and whenever the model is not
available – a plain summary built from the same data is sent instead.
"""

import html
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kickbase import models as m
from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.llm.client import LlmClient
from app.news.service import ReportView, reports_for_players
from app.openligadb.client import OpenLigaDbClient
from app.services.recommendations import (
    Recommendation,
    buy_view,
    change_percent,
    load_basis,
    sell_view,
)
from app.web.formatting import local_time, money_short, percent

TOP = 3
MAX_LINES = 8
NEWS_DAYS = 7
# Small whole numbers ("2 Spieler", "Top 3") may appear without being in the data.
FREE_NUMBER_LIMIT = 11
# Sign included: "−20" in the text must not pass for "+20" in the data. Both the ASCII minus and
# U+2212 (used by our formatting) count as minus. A sign only counts at the start of a word, so
# the hyphen in a range like "3-4" is no minus.
NUMBER = re.compile(r"(?<![\w.,])([+\-\u2212]?)(\d+(?:[.,]\d+)*)")

SYSTEM = """Du schreibst ein kurzes Morgen-Briefing für einen Kickbase-Manager (Fantasy-Fußball).
Regeln:
- Deutsch, höchstens 6 kurze Zeilen, jede Zeile eine konkrete Handlung oder ein wichtiger Hinweis
  (kaufen, verkaufen, aufstellen, Ausfälle im Kader).
- Nutze ausschließlich die Daten aus der Eingabe. Übernimm Namen und Zahlen exakt so, wie sie
  dort stehen; rechne nichts aus, runde nicht, erfinde nichts.
- Keine Begrüßung, keine Zeile ohne Inhalt, keine Formatierung (kein Markdown, keine Emojis)."""


class BriefingText(BaseModel):
    zeilen: list[str]


@dataclass(frozen=True)
class BriefingData:
    payload: dict[str, Any]
    fallback_lines: tuple[str, ...]
    # Data-quality hints (e.g. "Programm zählt neutral"): always sent as fixed lines.
    notes: tuple[str, ...] = ()

    @property
    def payload_json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


def _news_text(report: ReportView, tz: str, now: datetime) -> str:
    details = ", ".join(x for x in (report.absence, report.expected_return) if x)
    when = local_time(report.published_at, tz, now)
    if report.updated_at:
        when += (
            f"; Änderung erkannt {local_time(report.updated_at, tz, now)}"
            f" · Revision {report.revision}"
        )
    text = f"{report.status_label} ({report.kind_label}"
    return text + (f": {details}" if details else "") + f"; kicker {when})"


def _components(rec: Recommendation) -> str:
    points = rec.score.shown_points
    return ", ".join(f"{c.label} {points[c.key]:+d}" for c in rec.score.components)


async def collect_briefing_data(
    client: KickbaseClient,
    oldb: OpenLigaDbClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    tz: str,
    now: datetime,
) -> BriefingData:
    basis = await load_basis(client, oldb, sessions, league, now=now)
    buy = await buy_view(client, sessions, league, basis, now=now)
    sell = await sell_view(client, league, basis, now=now)
    squad = (await client.squad(league.id)).data.players
    news = await reports_for_players(
        sessions, [p.id for p in squad], now - timedelta(days=NEWS_DAYS)
    )
    teams = basis.team_names

    squad_rows = []
    for p in sorted(squad, key=lambda p: (p.position, p.name)):
        row = {
            "name": p.name,
            "verein": teams.get(p.team_id, p.team_id),
            "position": m.Position(p.position).short_label,
            "status": m.status_label(p.status),
            "marktwert": money_short(p.market_value),
        }
        trend = change_percent(p.market_value, p.market_value_change_7d)
        if trend is not None:
            row["trend_7_tage"] = percent(trend)
        if p.id in news:
            row["meldung"] = _news_text(news[p.id][0], tz, now)
        squad_rows.append(row)

    buys = [
        {
            "name": r.player.name,
            "verein": r.player.team_name or "",
            "position": r.player.position.short_label,
            "preis": money_short(r.price or 0),
            "score": r.score.shown_total,
            "bestandteile": _components(r),
            "angebot_endet": local_time(r.expires_at, tz, now) if r.expires_at else "",
        }
        for r in buy.recommendations[:TOP]
    ]
    sells = [
        {
            "name": r.player.name,
            "verein": r.player.team_name or "",
            "score": r.score.shown_total,
            "gruende": "; ".join(r.reasons),
        }
        for r in sell.recommendations[:TOP]
    ]
    local_now = now.astimezone(ZoneInfo(tz))
    payload = {
        "datum": f"{local_now.day}.{local_now.month}.",
        "kontostand": money_short(sell.budget),
        "kader": squad_rows,
        "kaufempfehlungen": buys,
        "verkaufskandidaten": sells,
    }
    if sell.budget_after_offers != sell.budget:
        payload["nach_offenen_geboten"] = money_short(sell.budget_after_offers)
    if sell.minus_if_bids_win is not None:
        payload["minus_falls_alle_gebote_gewinnen"] = money_short(sell.minus_if_bids_win)
    if sell.guide is not None:
        # The least-points-lost way when there is one, else the fewest sales.
        option = sell.guide.best or sell.guide.fewest
        payload["im_minus"] = {
            "fehlt": money_short(sell.guide.deficit),
            "vorschlag": ", ".join(f"{p.name} ({money_short(p.value)})" for p in option.players),
            "verliert_punkte_schnitt": option.lost_average,
        }
    return BriefingData(payload, tuple(_fallback_lines(payload)), tuple(basis.all_notes))


def _fallback_lines(payload: dict[str, Any]) -> list[str]:
    budget = f"Kontostand: {payload['kontostand']}"
    if "nach_offenen_geboten" in payload:
        budget += f" (nach allen offenen Geboten {payload['nach_offenen_geboten']})"
    lines = [budget]
    if payload["kaufempfehlungen"]:
        buys = ", ".join(
            f"{b['name']} ({b['preis']}, Score {b['score']})" for b in payload["kaufempfehlungen"]
        )
        lines.append(f"Kaufen: {buys}")
    if payload["verkaufskandidaten"]:
        sells = ", ".join(f"{s['name']} ({s['gruende']})" for s in payload["verkaufskandidaten"])
        lines.append(f"Verkaufen prüfen: {sells}")
    if "im_minus" in payload:
        minus = payload["im_minus"]
        lines.append(f"Im Minus ({minus['fehlt']} fehlen): verkaufe z. B. {minus['vorschlag']}.")
    elif "minus_falls_alle_gebote_gewinnen" in payload:
        lines.append(
            f"Gewinnen alle offenen Gebote, bist du {payload['minus_falls_alle_gebote_gewinnen']} "
            "im Minus – vor dem Spieltag verkaufen oder Gebote zurückziehen."
        )
    absent = [f"{p['name']}: {p['meldung']}" for p in payload["kader"] if "meldung" in p]
    if absent:
        lines.append("Meldungen im Kader: " + "; ".join(absent))
    return lines


def _negative(sign: str) -> bool:
    return sign in ("-", "\u2212")


def _values(token: str) -> set[Decimal]:
    """The values a number may mean: German ("1.234,5", "17,00") and, with a single dot and no
    comma, English ("8.3"). Compared by value, so "17" matches "17,00"."""
    values = set()
    with suppress(InvalidOperation):
        values.add(Decimal(token.replace(".", "").replace(",", ".")))
    if token.count(".") == 1 and "," not in token:
        with suppress(InvalidOperation):
            values.add(Decimal(token))
    return values


def unknown_numbers(text: str, source: str) -> list[str]:
    """Numbers in `text` that don't occur in `source` (apart from small whole numbers),
    compared by value. An unsigned number may stand for a signed one ("8,3 %" for "+8,3 %"),
    but a sign must match."""
    known = {
        (_negative(sign), value)
        for sign, token in NUMBER.findall(source)
        for value in _values(token)
    }
    any_sign = {value for _, value in known}
    unknown = []
    for sign, token in NUMBER.findall(text):
        if not sign and token.isdigit() and int(token) <= FREE_NUMBER_LIMIT:
            continue
        values = _values(token)
        if sign:
            ok = any((_negative(sign), v) in known for v in values)
        else:
            ok = any(v in any_sign for v in values)
        if not ok:
            unknown.append(sign + token)
    return unknown


@dataclass(frozen=True)
class Briefing:
    lines: tuple[str, ...]
    by_ai: bool
    note: str


async def write_briefing(llm: LlmClient, data: BriefingData) -> Briefing:
    """Lines by the model if they pass the number check, otherwise the plain fallback. Raises
    LlmError when the model can't be asked at all (the caller then sends the fallback)."""
    completion = await llm.complete("briefing", SYSTEM, data.payload_json, "briefing", BriefingText)
    lines = tuple(line.strip() for line in completion.data.zeilen if line.strip())[:MAX_LINES]
    if not lines:
        return Briefing(data.fallback_lines, False, "KI lieferte keinen Text")
    unknown = unknown_numbers("\n".join(lines), data.payload_json)
    if unknown:
        return Briefing(
            data.fallback_lines, False, f"KI-Text verworfen, unbekannte Zahlen: {unknown[:5]}"
        )
    return Briefing(lines, True, f"KI-Text von {completion.model}")


def compose_briefing(
    league_name: str, briefing: Briefing, notes: list[str] | tuple[str, ...], link: str
) -> str:
    lines = [f"<b>Briefing {html.escape(league_name)}</b>"]
    lines += [html.escape(line) for line in briefing.lines]
    lines += [f"⚠️ {html.escape(note)}" for note in notes]
    origin = "KI-Text aus den Dashboard-Daten" if briefing.by_ai else "ohne KI"
    lines.append(f'<i>{origin}</i> – <a href="{html.escape(link)}">Empfehlungen öffnen</a>')
    return "\n".join(lines)
