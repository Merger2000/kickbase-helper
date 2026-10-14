"""Weekday trading and a conservative financing plan for the actual lineup deadline.

All movements are observed. A constant daily change is only a scenario, never a prediction.
No orders are placed. Funding counts open bids and never counts hypothetical future profits.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.services.holding import holding_signal
from app.services.momentum import change_percent, daily_change
from app.services.recommendations import Basis, Recommendation, buy_view, eleven_view
from app.services.sell_guide import (
    GuideWeights,
    SalePlayer,
    SellGuide,
    SellOption,
    build_sell_guide,
)
from app.services.transfers import BidModel


@dataclass(frozen=True)
class HoldingRow:
    id: str
    name: str
    value: int
    delta: int | None
    percent: float | None
    gain: int | None
    protected: bool
    action: str
    reasons: tuple[str, ...]
    role: str = "auto"


@dataclass(frozen=True)
class TradeBuy:
    rec: Recommendation
    cost: int
    daily_gain: int
    updates: int
    break_even_updates: int
    scenario_profit: int


@dataclass(frozen=True)
class TradingView:
    now: datetime
    deadline: datetime | None
    sell_by: datetime | None
    closing: bool
    budget: int
    budget_after_offers: int
    protected: tuple[HoldingRow, ...]
    holdings: tuple[HoldingRow, ...]
    opportunities: tuple[TradeBuy, ...]
    guide: SellGuide | None
    funding: SellOption | None
    touches_eleven: bool
    open_slots: int
    bid_hint: str | None

    @property
    def deficit(self) -> int:
        return max(0, -min(self.budget, self.budget_after_offers))


def update_count(
    next_update: datetime | None, acquired_at: datetime, sell_by: datetime
) -> int | None:
    """Count known daily update opportunities after acquisition and before the safety cutoff.

    Do not invent an update time if the API supplies none or a stale timestamp.
    """
    if next_update is None:
        return None
    first = next_update
    while first <= acquired_at:
        first += timedelta(days=1)
    if first >= sell_by:
        return 0
    return ceil((sell_by - first).total_seconds() / 86400)


def trade_opportunity(
    rec: Recommendation,
    *,
    available: int,
    next_update: datetime | None,
    sell_by: datetime,
    now: datetime,
    min_gain: int,
) -> TradeBuy | None:
    movement = rec.momentum
    if next_update is None or next_update <= now:
        return None  # an expired API update time is not a known future schedule
    # A manager decides when to accept; only a timed Kickbase listing supports this scenario.
    if (
        not rec.seller_is_kickbase
        or rec.seller != "Kickbase"
        or rec.expires_at is None
        or rec.expires_at >= sell_by
    ):
        return None
    if movement is None or movement.change_24h is None or movement.change_24h < max(1, min_gain):
        return None
    if rec.price is None or rec.price <= 0:
        return None
    cost = max(rec.price, rec.my_offer or 0, rec.bid.amount if rec.bid else 0)
    if cost > available + (rec.my_offer or 0):
        return None
    updates = update_count(next_update, max(now, rec.expires_at), sell_by)
    if updates is None or updates == 0:
        return None
    premium = max(0, cost - rec.market_value)
    break_even = ceil(premium / movement.change_24h)
    profit = rec.market_value + updates * movement.change_24h - cost
    if profit <= 0:
        return None
    return TradeBuy(rec, cost, movement.change_24h, updates, break_even, profit)


async def trading_view(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    basis: Basis,
    bids: BidModel | None = None,
    bid_hint: str | None = None,
    now: datetime | None = None,
) -> TradingView:
    now = now or datetime.now(UTC)
    buys = await buy_view(client, sessions, league, basis, bids, bid_hint, now)
    eleven = await eleven_view(client, league, basis)
    squad = (await client.squad(league.id)).data
    budget = (await client.budget(league.id)).data
    market = (await client.market(league.id)).data
    cfg = basis.config.trading
    deadline = buys.deadline
    sell_by = deadline - timedelta(hours=cfg.safety_hours) if deadline else None
    closing = deadline is None or deadline - now <= timedelta(hours=cfg.close_hours)
    manual_ids = {pid for pid, role in basis.holding_roles.items() if role == "core"}
    protected_ids = {r.player.id for line in eleven.lines for r in line.players} | manual_ids
    recs = {r.player.id: r for line in eleven.lines for r in line.players}
    recs |= {r.player.id: r for r in (*eleven.bench, *eleven.risky, *eleven.excluded)}
    protected, holdings, sale, flexible = [], [], [], []
    for player in squad.players:
        start = recs[player.id].start
        signal = holding_signal(
            current=player.market_value,
            change_24h=daily_change(player),
            start=start,
            deadline=deadline,
            now=now,
            config=cfg,
        )
        core = player.id in protected_ids
        role = basis.holding_roles.get(player.id, "auto")
        action = (
            "Von dir geschützt"
            if role == "core"
            else ("Für die Elf halten" if core else signal.verdict)
        )
        if closing and not core:
            action = "Vor Deadline prüfen / Gewinn sichern"
        row = HoldingRow(
            player.id,
            player.name,
            player.market_value,
            daily_change(player),
            change_percent(player.market_value, daily_change(player)),
            player.market_value_gain if "market_value_gain" in player.model_fields_set else None,
            core,
            action,
            signal.reasons,
            role,
        )
        (protected if core else holdings).append(row)
        sale_player = SalePlayer(
            player.name,
            player.market_value,
            player.average_points,
            start.percent,
            player.position,
            start.percent == 0,
            player.id,
        )
        sale.append(sale_player)
        if not core:
            flexible.append(sale_player)
    weights = basis.config.sell_guide
    guide = build_sell_guide(
        min(budget.budget, budget.budget_after_offers),
        sale,
        flexible,
        GuideWeights(
            weights.season_weight,
            weights.matchday_weight,
            weights.empty_slot_points,
            weights.max_players,
        ),
        protected_ids=manual_ids,
    )
    funding = (
        None if guide is None else (guide.candidates if guide.candidates.covers else guide.best)
    )
    flexible_names = {p.name for p in flexible}
    touches_eleven = bool(funding and any(p.name not in flexible_names for p in funding.players))
    opportunities = []
    if not closing and sell_by is not None and sell_by > now:
        for rec in buys.scored:
            candidate = trade_opportunity(
                rec,
                available=buys.budget,
                next_update=market.next_market_value_update,
                sell_by=sell_by,
                now=now,
                min_gain=cfg.min_daily_gain,
            )
            if candidate:
                opportunities.append(candidate)
    opportunities.sort(key=lambda p: (p.scenario_profit, p.daily_gain), reverse=True)
    holdings.sort(key=lambda p: (p.delta is None, -(p.delta or 0)))
    return TradingView(
        now,
        deadline,
        sell_by,
        closing,
        budget.budget,
        budget.budget_after_offers,
        tuple(protected),
        tuple(holdings),
        tuple(opportunities[:10]),
        guide,
        funding,
        touches_eleven,
        sum(line.empty_slots for line in eleven.lines),
        bid_hint,
    )
