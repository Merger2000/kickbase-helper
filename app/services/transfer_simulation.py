"""Local transfer scenarios. Budget and lineup effects never place an order in Kickbase."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.kickbase.models import Budget
from app.services.recommendations import (
    Basis,
    ElevenView,
    Recommendation,
    build_eleven,
    buy_view,
    eleven_view,
)
from app.services.scoring import Profile
from app.services.transfers import BidModel


class SimulationError(ValueError):
    """The requested scenario no longer matches the cached roster/market."""


@dataclass(frozen=True)
class PlanOffer:
    rec: Recommendation
    cost: int
    own_bid: int
    before_deadline: bool
    manager_sale: bool = False


@dataclass(frozen=True)
class TransferResult:
    sold: tuple[Recommendation, ...]
    bought: tuple[PlanOffer, ...]
    revenue: int
    cost: int
    released_bids: int
    remaining_bids: int
    budget: int
    budget_after_bids: int
    roster_size: int
    before: ElevenView
    after: ElevenView

    @property
    def gaps(self) -> int:
        return sum(line.empty_slots for line in self.after.lines)

    @property
    def deficit(self) -> int:
        return max(0, -min(self.budget, self.budget_after_bids))


@dataclass(frozen=True)
class SimulationView:
    squad: tuple[Recommendation, ...]
    offers: tuple[PlanOffer, ...]
    budget: int
    reserved_bids: int
    deadline: datetime | None
    result: TransferResult | None
    error: str | None
    warnings: tuple[str, ...]


def simulate_transfers(
    squad: Sequence[Recommendation],
    offers: Sequence[PlanOffer],
    budget: int,
    reserved_bids: int,
    sales: Sequence[str],
    purchases: Sequence[str],
    costs: Mapping[str, int],
    profile: Profile,
) -> TransferResult:
    """Count all pending bids once, credit selected existing bids only within that reserve."""
    own = {r.player.id: r for r in squad}
    market = {o.rec.player.id: o for o in offers}
    if len(set(sales)) != len(sales) or len(set(purchases)) != len(purchases):
        raise SimulationError("Ein Spieler wurde mehrfach ausgewählt. Auswahl bitte prüfen.")
    if set(sales) - own.keys():
        raise SimulationError("Ein ausgewählter Verkauf ist nicht mehr im Kader. Auswahl prüfen.")
    if set(purchases) - market.keys():
        raise SimulationError("Ein ausgewähltes Angebot ist nicht mehr verfügbar. Auswahl prüfen.")
    if set(purchases) & own.keys():
        raise SimulationError("Ein eigener Spieler kann nicht erneut als Kauf eingeplant werden.")
    sold = tuple(own[pid] for pid in sales)
    bought = []
    for pid in purchases:
        offer = market[pid]
        amount = costs.get(pid, offer.cost)
        minimum = max(1, offer.rec.price or 0, offer.own_bid)
        if not minimum <= amount <= 999_999_999:
            raise SimulationError(
                f"Kaufpreis für {offer.rec.player.name} muss mindestens Angebotspreis und "
                "bestehendes Gebot decken (maximal 999.999.999 €)."
            )
        bought.append(replace(offer, cost=amount))
    revenue = sum(r.market_value for r in sold)
    cost = sum(o.cost for o in bought)
    released = min(max(0, reserved_bids), sum(o.own_bid for o in bought))
    remaining = max(0, reserved_bids - released)
    projected = [r for r in squad if r.player.id not in set(sales)]
    # Unconfirmed or late transfers must not silently fill holes for the coming matchday.
    eligible = [o.rec for o in bought if o.before_deadline]
    before = build_eleven(list(squad), {}, profile)
    after = build_eleven(projected + eligible, {}, profile)
    return TransferResult(
        sold,
        tuple(bought),
        revenue,
        cost,
        released,
        remaining,
        budget + revenue - cost,
        budget + revenue - cost - remaining,
        len(projected) + len(bought),
        before,
        after,
    )


def reserve_from(budget: Budget, known_bids: int) -> tuple[int, str | None]:
    reserve = max(0, budget.budget - budget.budget_after_offers)
    warning = None
    if known_bids > reserve or budget.budget_after_offers > budget.budget:
        warning = (
            "Budget und bekannte Gebote passen nicht vollständig zusammen. "
            "Die Rechnung reserviert vorsichtshalber den höheren bekannten Betrag."
        )
    return max(reserve, known_bids), warning


async def simulation_view(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    basis: Basis,
    sales: Sequence[str] = (),
    purchases: Sequence[str] = (),
    costs: Mapping[str, int] | None = None,
    bids: BidModel | None = None,
    bid_hint: str | None = None,
    now: datetime | None = None,
) -> SimulationView:
    now = now or datetime.now(UTC)
    baseline = await eleven_view(client, league, basis)
    buys = await buy_view(client, sessions, league, basis, bids, bid_hint, now)
    budget = (await client.budget(league.id)).data
    market = (await client.market(league.id)).data
    squad = tuple(
        [r for line in baseline.lines for r in line.players]
        + list(baseline.bench)
        + list(baseline.risky)
        + list(baseline.excluded)
    )
    own_ids = {r.player.id for r in squad}
    offers = []
    listings = {p.id: p for p in market.players}
    for rec in buys.scored:
        if rec.player.id in own_ids or not rec.price or rec.price < 0:
            continue
        rec = replace(rec, start=basis.start_probability(rec.player))
        manager_sale = listings[rec.player.id].seller is not None
        timed = (
            buys.deadline is not None
            and rec.expires_at is not None
            and now < rec.expires_at < buys.deadline
            and not manager_sale
        )
        offers.append(
            PlanOffer(
                rec,
                max(rec.price, rec.my_offer or 0, rec.bid.amount if rec.bid else 0),
                max(0, rec.my_offer or 0),
                timed,
                manager_sale,
            )
        )
    # Includes bids whose listings aren't suitable for a matchday purchase.
    known_bids = sum(max(0, p.my_offer or 0) for p in market.players)
    reserved, warning = reserve_from(budget, known_bids)
    result, error = None, None
    try:
        result = simulate_transfers(
            squad,
            offers,
            budget.budget,
            reserved,
            sales,
            purchases,
            costs or {},
            basis.config.profiles["aufstellung"],
        )
    except SimulationError as exc:
        error = str(exc)
    protected_sold = [
        r.player.name
        for r in squad
        if r.player.id in sales and basis.holding_roles.get(r.player.id) == "core"
    ]
    manual_warning = (
        "Dein Plan verkauft geschützte Stammspieler: " + ", ".join(protected_sold)
        if protected_sold
        else None
    )
    warnings = tuple(filter(None, [warning, bid_hint, manual_warning]))
    return SimulationView(
        squad,
        tuple(sorted(offers, key=lambda o: o.cost)),
        budget.budget,
        reserved,
        buys.deadline,
        result,
        error,
        warnings,
    )
