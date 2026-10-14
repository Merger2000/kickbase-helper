"""The "Lagebild": a compact text of the user's situation that goes into every chat question.

A small model answers far better when the relevant data is already in the prompt than when it has
to pick tools one by one. Only the own league data and public Bundesliga data go in – never the
names of other managers (the free AI tier may train on inputs, see AGENTS.md).
"""

from dataclasses import dataclass, field
from datetime import datetime
from logging import getLogger
from zoneinfo import ZoneInfo

from app.kickbase.league import ResolvedLeague
from app.services.matchday_history import load_matchday_history
from app.services.recommendations import (
    Basis,
    BuyFilters,
    BuyView,
    Recommendation,
    buy_view,
    change_percent,
    eleven_view,
    load_basis,
    select_buys,
    sell_view,
    still_listed,
)
from app.services.team import load_team
from app.services.today import LINEUP_SIZE, TodayView, lineup_risks
from app.services.transfers import load_bid_status
from app.services.watchlist import list_entries
from app.web.formatting import money_short, percent

logger = getLogger(__name__)

WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
BUY_LINES = 6


@dataclass
class Situation:
    text: str
    # Reused by the tools so a follow-up search doesn't load everything again.
    basis: Basis | None = None
    buys: BuyView | None = None
    squad_ids: set[str] = field(default_factory=set)


def _when(value: datetime, tz: ZoneInfo) -> str:
    local = value.astimezone(tz)
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m. %H:%M}"


def _player(r: Recommendation) -> str:
    club = f", {r.player.team_name}" if r.player.team_name else ""
    return f"{r.player.name} ({r.player.position.short_label}{club})"


def buy_line(r: Recommendation) -> str:
    """One market offer; the seller is left out on purpose (another manager's name)."""
    parts = [f"Preis {money_short(r.price)}" if r.price is not None else "Preis ?"]
    parts.append(f"Marktwert {money_short(r.market_value)}")
    if r.my_offer:
        parts.append(f"DEIN GEBOT {money_short(r.my_offer)}")
    elif r.bid:
        parts.append(f"Gebotsempfehlung {money_short(r.bid.amount)}")
    parts.append(f"Score {r.score.shown_total}")
    if r.xp is not None:
        parts.append(f"Aufstellungsindex {round(r.xp.value * 100)}/100 (keine Punkteprognose)")
    if r.momentum and r.momentum.change_24h is not None:
        parts.append(f"Marktwert 24 h {money_short(r.momentum.change_24h, signed=True)}")
    if r.before_deadline is True:
        parts.append("rechtzeitig vor der Deadline im Kader")
    elif r.before_deadline is False:
        parts.append("erst NACH der Deadline im Kader")
    if r.player.status_label != "fit":
        parts.append(f"Status {r.player.status_label}")
    return f"- {_player(r)}: " + ", ".join(parts)


async def build_situation(ctx, tz: ZoneInfo) -> Situation:
    """Everything the dashboard knows that matters for a decision, as plain German text.
    `ctx` is the bot's ToolContext (client, sessions, league, oldb, now)."""
    client, sessions, league, now = ctx.client, ctx.sessions, ctx.league, ctx.now
    league: ResolvedLeague
    basis = await load_basis(client, ctx.oldb, sessions, league, now)
    bids = await load_bid_status(sessions, league.id, now, basis.config.bids)
    buys = await buy_view(client, sessions, league, basis, bids.model, bids.hint, now)
    sell = await sell_view(client, league, basis, now=now)
    eleven = await eleven_view(client, league, basis)
    team = await load_team(client, league)
    lineup = (await client.my_eleven(league.id)).data
    squad = (await client.squad(league.id)).data
    budget = (await client.budget(league.id)).data
    market = await client.market(league.id)

    out: list[str] = [f"Jetzt: {_when(now, tz)} (deutsche Zeit)."]
    view = TodayView(now=now, deadline=buys.deadline, matchday=market.data.next_matchday)
    if view.deadline:
        md = f" für den {view.matchday}. Spieltag" if view.matchday else ""
        out.append(
            f"Nächste Aufstellungs-Deadline{md}: {_when(view.deadline, tz)} ({view.countdown})."
        )
    else:
        out.append("Keine Aufstellungs-Deadline angekündigt.")

    konto = f"Kontostand: {money_short(budget.budget)}"
    if budget.budget < 0:
        konto += " – IM MINUS: solange das Konto negativ ist, gibt es am Spieltag keine Punkte"
    if budget.budget_after_offers != budget.budget:
        konto += f"; falls alle offenen Gebote gewinnen: {money_short(budget.budget_after_offers)}"
    out.append(konto + ".")
    if team.placement:
        out.append(
            f"Liga: Platz {team.placement} von {team.manager_count}, Teamwert "
            f"{money_short(team.team_value)}."
        )

    # Current lineup and what is wrong with it.
    lined_up = {p.id for p in lineup.players}
    out.append(f"\nAKTUELLE AUFSTELLUNG ({len(lineup.players)} von {LINEUP_SIZE} Plätzen):")
    out.append(", ".join(p.name for p in lineup.players) or "niemand aufgestellt")
    if empty := LINEUP_SIZE - len(lineup.players):
        out.append(f"{empty} Plätze leer – leere Plätze bringen keine Punkte.")
    squad_by_id = {p.id: p for p in squad.players}
    for risk in lineup_risks(
        lineup.players, squad_by_id, basis.predicted, basis.injuries, basis.quotes
    ):
        out.append(f"Risiko: {risk.headline} ({', '.join(risk.reasons)})")
    line_up = ", ".join(
        f"{line.position.short_label}: "
        + ", ".join(r.player.name for r in line.players)
        + (f" (+{line.empty_slots} leer)" if line.empty_slots else "")
        for line in eleven.lines
    )
    out.append(
        f"Beste Elf nach Aufstellungsindex ({eleven.formation.label}, "
        f"Index-Summe {round(eleven.total_index)}; keine Punkteprognose): {line_up}."
    )
    if eleven.coming_in or eleven.going_out:
        rein = ", ".join(r.player.name for r in eleven.coming_in) or "–"
        raus = ", ".join(eleven.going_out) or "–"
        out.append(f"Änderungen dafür: rein {rein}; raus {raus}.")
    else:
        out.append("Die aktuelle Aufstellung ist schon die beste aus dem Kader.")

    # Squad with the keep/sell view.
    out.append("\nKADER (Score 0–100 fürs Halten, Trend = Marktwert 7 Tage):")
    for r in sorted(sell.scored, key=lambda r: r.score.total):
        sp = squad_by_id.get(r.player.id)
        trend = change_percent(sp.market_value, sp.market_value_change_7d) if sp else None
        start = basis.start_probability(r.player)
        parts = [
            f"Marktwert {money_short(r.market_value)}",
            f"Trend {percent(trend)}" if trend is not None else "Trend ?",
            f"Startchance {start.percent} %" if start.percent is not None else "Startchance ?",
            f"Score {r.score.shown_total}",
        ]
        if r.gain is not None:
            parts.append(f"seit Kauf {money_short(r.gain, signed=True)}")
        if r.player.status_label != "fit":
            parts.append(f"Status {r.player.status_label}")
        if r.player.id in lined_up:
            parts.append("aufgestellt")
        flag = f" → VERKAUFSKANDIDAT ({'; '.join(r.reasons)})" if r.reasons else ""
        out.append(f"- {_player(r)}: " + ", ".join(parts) + flag)
    if buys.gaps:
        gaps = ", ".join(f"{n}× {pos.short_label}" for pos, n in buys.gaps.items())
        out.append(f"Kaderlücken (einsatzfähige Spieler fehlen): {gaps}.")

    if sell.guide:
        g = sell.guide
        out.append(
            f"\nAUS DEM MINUS: es fehlen {money_short(g.deficit)}. Wege (Punkteverlust: kleiner "
            "ist besser, aus Saison-Ø, erwarteten Punkten am nächsten Spieltag, leeren Plätzen):"
        )
        options = [("EMPFEHLUNG, geringster Punkteverlust", g.best)] if g.best else []
        options += [("Wenigste Verkäufe", g.fewest), ("Nur Verkaufskandidaten", g.candidates)]
        for label, option in options:
            if not option.players:
                continue
            names = " + ".join(
                f"{p.name} ({money_short(p.value)}, Ø {p.average_points})" for p in option.players
            )
            after = (
                f"danach Konto {money_short(option.surplus, signed=True)}"
                if option.covers
                else "reicht NICHT"
            )
            empty = (
                f", danach {option.newly_empty} Plätze mehr in der Elf leer"
                if option.newly_empty
                else ""
            )
            out.append(
                f"- {label}: {names} = {money_short(option.total)}, {after}, "
                f"Punkteverlust {option.points_lost}{empty}."
            )

    # Market.
    shown, matching = select_buys(buys, BuyFilters())
    out.append(
        f"\nKAUFEMPFEHLUNGEN (im Budget {money_short(buys.budget)}, Score ≥ "
        f"{buys.min_score:g}, vor der Deadline; {matching} passen):"
    )
    out += [buy_line(r) for r in shown[:BUY_LINES]] or ["- keine"]
    if buys.over_budget_recs:
        out.append(f"Gute Angebote ÜBER dem Budget ({buys.over_budget}):")
        out += [buy_line(r) for r in buys.over_budget_recs[:3]]
    mine = [p for p in still_listed(market, now) if p.my_offer is not None]
    if mine:
        out.append("Deine offenen Gebote:")
        for p in mine:
            end = p.expires_at(market.fetched_at)
            ends = f", endet {_when(end, tz)}" if end else ""
            out.append(
                f"- {p.first_name} {p.last_name}".rstrip()
                + f": Gebot {money_short(p.my_offer)}, Preis {money_short(p.price)}{ends}"
            )

    # Watchlist (Block C, C1.5). Reuses the market + squad already fetched above, so this
    # section adds no Kickbase calls. Skipped entirely when the user has no entries – the
    # AI doesn't need to know the concept exists. Never mentions sellers or other managers.
    try:
        watch = await list_entries(sessions, league.id)
    except Exception as exc:
        logger.warning("Merkliste für Lagebild nicht ladbar: %s: %s", type(exc).__name__, exc)
        watch = []
    if watch:
        market_by_id = {p.id: p for p in market.data.players}
        out.append("\nMERKLISTE (nicht im Kader – Alarme siehe /merkliste):")
        for item in watch:
            listed = market_by_id.get(item.entry.player_id)
            parts: list[str] = []
            if listed is not None:
                parts.append(f"auf Markt ja, Preis {money_short(listed.price)}")
            else:
                parts.append("auf Markt nein")
            if item.status_label != "fit":
                parts.append(f"Status {item.status_label}")
            if item.entry.price_below is not None:
                parts.append(f"Schwelle {money_short(item.entry.price_below)}")
            out.append(f"- {item.player_name}: " + ", ".join(parts))

    # Own-offers (Block C, C2.4): listings by the manager that already received bids. One
    # bullet per own listing with at least one bid: name, best bid, market value, number of
    # bids. Bidder names are private and must never reach the LLM (free-tier may train on
    # inputs). Skipped when nothing of mine is bid on – the AI doesn't need to know the
    # concept exists.
    try:
        my_user_id = (await client.user_me()).data.user.id
        own_listings = [
            p
            for p in market.data.players
            if p.seller is not None and p.seller.id == my_user_id and p.best_offer is not None
        ]
    except Exception as exc:
        logger.warning("Eigene Angebote für Lagebild nicht ladbar: %s: %s", type(exc).__name__, exc)
        own_listings = []
    if own_listings:
        out.append("\nGEBOTE AUF MEINE SPIELER (annehmen nur in der Kickbase-App):")
        for p in own_listings:
            best = p.best_offer
            assert best is not None  # filter above guarantees this
            out.append(
                f"- {p.first_name} {p.last_name}".rstrip()
                + f": bestes Gebot {money_short(best.amount)}"
                + f" (Marktwert {money_short(p.market_value)},"
                + f" {p.offer_count} Gebot{'e' if p.offer_count != 1 else ''})"
            )

    # Last matchday summary (Block C, C3.5). Pure DB read via the same service the team page
    # uses – no Kickbase call, no other managers' data (only the own row in
    # `matchday_results`). Skipped entirely when no matchday has finished yet – the AI doesn't
    # need to know the concept exists.
    try:
        history = await load_matchday_history(sessions, league.id)
    except Exception as exc:
        logger.warning(
            "Spieltags-Bilanz für Lagebild nicht ladbar: %s: %s", type(exc).__name__, exc
        )
        history = None
    if history and history.latest is not None:
        latest = history.latest
        delta = history.latest_placement_delta
        change = ""
        if delta is None:
            change = " – 1. Spieltag"
        elif delta > 0:
            change = f" (▲{delta})"
        elif delta < 0:
            change = f" (▼{abs(delta)})"
        else:
            change = " (±0)"
        parts = [
            f"{latest.points} Punkte",
            f"Platz {latest.placement}{change}",
        ]
        if latest.matchday_placement is not None:
            parts.append(f"Spieltagsplatz {latest.matchday_placement}")
        if latest.missed_points is not None:
            parts.append(f"verpasste Punkte {latest.missed_points}")
        out.append(f"\nLETZTER SPIELTAG ({latest.matchday}.): " + ", ".join(parts) + ".")

    notes = [*basis.all_notes, *([bids.hint] if bids.hint else [])]
    if notes:
        out.append("\nHINWEISE ZU DEN DATEN: " + " ".join(notes))
    return Situation("\n".join(out), basis, buys, set(squad_by_id))
