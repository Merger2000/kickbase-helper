"""Read-only tools the Telegram bot can call to answer questions from our own data."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.news.service import reports_for_players
from app.openligadb.client import OpenLigaDbClient
from app.services.players import load_player, search_players
from app.web.formatting import money_short, percent

PLAYER_ID = re.compile(r"\d{1,10}")
PLAYER_NAME = re.compile(r"[^\W\d_][\w .'\-]{1,59}")
logger = logging.getLogger(__name__)

TOOLS = ("spieler", "markt")
# Older names the model may still use: mapped to the new tools.
ALIASES = {
    "spieler_suchen": "spieler",
    "spieler_details": "spieler",
    "marktangebote": "markt",
    "kaufen": "markt",
}
MAX_ITEMS = 10
POSITION_WORDS = {
    1: ("tw", "torwart", "torhüter", "keeper"),
    2: ("abw", "abwehr", "verteidiger", "verteidigung", "iv", "av"),
    3: ("mf", "mittelfeld", "mittelfeldspieler"),
    4: ("st", "sturm", "stürmer", "angreifer", "angriff"),
}


@dataclass
class ToolContext:
    client: KickbaseClient
    sessions: async_sessionmaker
    league: ResolvedLeague
    oldb: OpenLigaDbClient | None
    now: datetime
    # The prepared situation (app/bot/context.py), reused by the market tool.
    situation: Any = None
    extra: dict = field(default_factory=dict)


async def run_tool(ctx: ToolContext, name: str, argument: str | None) -> str:
    """Execute one read-only tool and return a compact text result. Unknown tools and errors
    return a short message instead of raising, so the model can recover."""
    try:
        match ALIASES.get(name, name):
            case "spieler":
                return await _spieler(ctx, argument or "")
            case "markt":
                return await _markt(ctx, argument or "")
            case "spieler_suchen":
                return await _spieler_suchen(ctx, argument or "")
            case "spieler_details":
                return await _spieler_details(ctx, argument or "")
            case "marktangebote":
                return await _marktangebote(ctx)
            case "mein_kader":
                return await _mein_kader(ctx)
            case "budget":
                return await _budget(ctx)
            case _:
                return f"Unbekanntes Werkzeug '{name}'."
    except Exception as exc:  # a tool failure must not break the conversation
        logger.warning("Werkzeug %s fehlgeschlagen: %s", name, exc)
        return f"Werkzeug '{name}' fehlgeschlagen ({type(exc).__name__})."


async def _spieler_suchen(ctx: ToolContext, query: str) -> str:
    if not query.strip():
        return "Kein Suchbegriff angegeben."
    result = await search_players(ctx.sessions, query)
    if not result.players:
        return "Keine Spieler gefunden."
    lines = [f"{len(result.players)} Treffer:"]
    for p in result.players[:MAX_ITEMS]:
        lines.append(
            f"- {p.name} ({p.position.short_label}, {p.team_name or '?'}): "
            f"Marktwert {money_short(p.market_value)}, Status {p.status_label} (id {p.id})"
        )
    return "\n".join(lines)


async def _spieler_details(ctx: ToolContext, player_id: str) -> str:
    player_id = (player_id or "").strip()
    if not PLAYER_ID.fullmatch(player_id):
        # Only digits: the id ends up in a Kickbase URL path ("../" would leave it).
        return "Ungültige Spieler-ID (nur Ziffern, z. B. aus spieler_suchen)."
    page = await load_player(ctx.client, ctx.sessions, ctx.league, player_id)
    reports = await reports_for_players(ctx.sessions, [player_id], ctx.now - timedelta(days=30))
    news = reports.get(player_id, [])
    lines = [f"{page.name} ({page.position.short_label}, {page.team_name or '?'})"]
    lines.append(f"Status: {page.status_label}, Marktwert {money_short(page.market_value)}")
    if page.market_value_change_7d_percent is not None:
        lines.append(f"Marktwert 7 Tage: {percent(page.market_value_change_7d_percent)}")
    lines.append(
        f"Punkte: {page.total_points} gesamt, Ø {page.average_points}, "
        f"{page.goals} Tore, {page.assists} Vorlagen"
    )
    prob = page.start_probability
    if prob.percent is not None:
        lines.append(f"Startchance: {prob.percent} % ({'; '.join(prob.reasons)})")
    if page.prediction_hint:
        lines.append(f"Hinweis zu den Daten: {page.prediction_hint}")
    if page.injury is not None:
        reason = page.injury.reason or ""
        lines.append(f"Verletzung: {page.injury.label}{': ' + reason if reason else ''}")
    for match in page.upcoming[:3]:
        lines.append(f"Nächstes Spiel: {match.matchday}. Spieltag gegen {match.opponent or '?'}")
    if news:
        lines.append("Meldungen:")
        for r in news[:3]:
            detail = f" ({r.absence})" if r.absence else ""
            lines.append(f"- {r.kind_label}: {r.status_label}{detail}")
    if ctx.oldb is not None:
        # The same verdict as the player page, so the model never borrows another player's score.
        from app.services.decision import player_decision

        d = await player_decision(ctx.client, ctx.oldb, ctx.sessions, ctx.league, page, ctx.now)
        where = "IM EIGENEN KADER" if d.in_squad else "nicht im eigenen Kader"
        reasons = f" ({'; '.join(d.reasons)})" if d.reasons else ""
        lines.insert(1, f"{where}; Einschätzung: {d.verdict}, Score {d.score.shown_total}{reasons}")
    return "\n".join(lines)


async def _marktangebote(ctx: ToolContext) -> str:
    if ctx.oldb is None:
        return "Marktangebote nicht verfügbar (OpenLigaDB fehlt)."
    from app.services.recommendations import buy_view, load_basis
    from app.services.transfers import load_bid_status

    basis = await load_basis(ctx.client, ctx.oldb, ctx.sessions, ctx.league)
    bids = await load_bid_status(ctx.sessions, ctx.league.id, ctx.now, basis.config.bids)
    view = await buy_view(
        ctx.client, ctx.sessions, ctx.league, basis, bids.model, bids.hint, ctx.now
    )
    if not view.recommendations:
        return "Keine Empfehlungen auf dem Markt."
    lines = [f"Top-Käufe (Budget {money_short(view.budget)}):"]
    for r in view.recommendations[:MAX_ITEMS]:
        price = money_short(r.price) if r.price is not None else "?"
        lines.append(
            f"- {r.player.name} ({r.player.position.short_label}, {r.player.team_name or '?'}): "
            f"Preis {price}, Score {r.score.shown_total}"
        )
    return "\n".join(lines)


async def _mein_kader(ctx: ToolContext) -> str:
    squad = (await ctx.client.squad(ctx.league.id)).data.players
    if not squad:
        return "Kader leer."
    lines = ["Dein Kader:"]
    for p in squad:
        lines.append(
            f"- {p.name} ({p.position.short_label}): Marktwert {money_short(p.market_value)}, "
            f"Status {p.status_label}"
        )
    return "\n".join(lines)


async def _budget(ctx: ToolContext) -> str:
    budget = (await ctx.client.budget(ctx.league.id)).data
    text = f"Kontostand: {money_short(budget.budget)}" + (
        " (im Minus)" if budget.budget < 0 else ""
    )
    if budget.budget_after_offers != budget.budget:
        text += f"; nach allen offenen Geboten: {money_short(budget.budget_after_offers)}"
        if budget.budget >= 0 > budget.budget_after_offers:
            text += " (im Minus, falls alle Gebote gewinnen)"
    return text if text.endswith(".") else text + "."


async def _spieler(ctx: ToolContext, argument: str) -> str:
    """Details of one player by id or name (the model often passes the name)."""
    argument = argument.strip()
    if PLAYER_ID.fullmatch(argument):
        return await _spieler_details(ctx, argument)
    if not PLAYER_NAME.fullmatch(argument):
        # Neither an id nor a plausible name (e.g. "../ranking"): nothing reaches Kickbase.
        return "Ungültige Spieler-ID oder Name (Ziffern oder ein Spielername)."
    result = await search_players(ctx.sessions, argument)
    if not result.players:
        return f"Keinen Spieler zu '{argument}' gefunden."
    first = result.players[0]
    text = await _spieler_details(ctx, first.id)
    others = [f"{p.name} ({p.team_name or '?'})" for p in result.players[1:4]]
    if others:
        text += "\nWeitere Treffer: " + ", ".join(others)
    return text


def _parse_market_query(argument: str) -> tuple[int, int | None, bool]:
    """'ST bis 5 Mio', 'Abwehr 10', 'alle' → (position, max price in €, include over budget)."""
    words = re.findall(r"[\wäöüß]+(?:[.,]\d+)?", argument.lower())
    position = 0
    for pos, names in POSITION_WORDS.items():
        if any(w in names for w in words):
            position = pos
    max_price = None
    text = argument.lower()
    # A number is a price only as "5 Mio" or after "bis/unter/max/für" – not "7. Spieltag".
    price = re.search(r"(?:bis|unter|max\.?|maximal|für)\s*(\d+(?:[.,]\d+)?)", text) or re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:mio|m\b)", text
    )
    if price is None and re.fullmatch(r"\D*?(\d+(?:[.,]\d+)?)\s*", text):
        price = re.fullmatch(r"\D*?(\d+(?:[.,]\d+)?)\s*", text)  # a tool call like "ST 5"
    if price:
        max_price = round(float(price.group(1).replace(",", ".")) * 1_000_000)
    over_budget = any(w in ("alle", "über", "ueber", "budget") for w in words)
    return position, max_price, over_budget


async def _markt(ctx: ToolContext, argument: str) -> str:
    from app.bot.context import buy_line
    from app.services.recommendations import BuyFilters, select_buys

    buys = getattr(ctx.situation, "buys", None)
    if buys is None:
        return "Marktdaten gerade nicht verfügbar."
    position, max_price, over_budget = _parse_market_query(argument)
    filters = BuyFilters(
        position=position,
        max_price=max_price,
        over_budget=over_budget or max_price is not None,
        before_deadline=False,
    )
    shown, matching = select_buys(buys, filters)
    if not shown:
        return (
            f"Keine Angebote mit Score ≥ {buys.min_score:g} für diese Suche "
            f"(Budget {money_short(buys.budget)})."
        )
    head = f"{matching} passende Angebote (Score ≥ {buys.min_score:g}), beste zuerst:"
    return "\n".join([head, *(buy_line(r) for r in shown[:8])])


_STOPWORDS = """
    soll sollte kann wie was wer wen wem ist sind mein meine meinen meinem kader und oder der
    die das den dem des ein eine einen gerade aktuell spieler verkaufen kaufen aufstellen lohnt
    sich noch mehr gut drauf form budget markt heute morgen woche spieltag deadline minus"""
# Question words that are never player names (saves pointless searches).
STOPWORDS = frozenset(_STOPWORDS.split())
MAX_NAMED = 2


async def named_players(sessions, question: str) -> list[str]:
    """Player names mentioned in the question ("Wie ist Kane drauf?" → ["Kane"]), matched against
    the collected players (whole name part, so "Kim" doesn't pick "Kimmich")."""
    found: list[str] = []
    words = re.findall(r"[^\W\d_][\w'\-]{2,}", question)
    for word in words:
        if word.lower() in STOPWORDS or len(found) >= MAX_NAMED:
            continue
        result = await search_players(sessions, word)
        for p in result.players[:5]:
            parts = re.split(r"[\s\-]+", p.name.lower())
            if word.lower() in parts and word not in found:
                found.append(word)
                break
    return found
