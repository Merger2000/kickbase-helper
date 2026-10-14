"""Own bids: seen on the market (`uop`, per listing), evaluated after the offer ended.

Outcome: won (the player is in the own squad), lost (another manager bought him; the winning
price comes from the league transfers), none (no purchase within a few hours after the end, e.g.
the offer was withdrawn or nobody bought). The statistics show how far lost bids were off, which
is the missing half of the bid model: the feed only shows winning prices.
"""

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import LeagueTransfer, MyOffer
from app.kickbase import models as m
from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague

logger = logging.getLogger(__name__)

# A purchase is matched to the offer when it happened from (first seen − slack) on.
MATCH_SLACK = timedelta(hours=1)
# Without any purchase this long after the end, the offer ended without a sale.
NO_SALE_AFTER = timedelta(hours=6)
# Manager listings have no end: the last time they were still listed counts as the end.
STATS_DAYS = 60


@dataclass(frozen=True)
class TrackResult:
    new: int
    evaluated: int


async def track_offers(
    client: KickbaseClient, sessions: async_sessionmaker, league: ResolvedLeague, now: datetime
) -> TrackResult:
    market = await client.market(league.id, refresh=True)
    listed = {p.listing_key: p for p in market.data.players}
    new = 0
    async with sessions.begin() as session:
        for player in market.data.players:
            if not player.my_offer:
                continue
            offer = await session.get(MyOffer, player.listing_key)
            if offer is None:
                session.add(
                    MyOffer(
                        offer_id=player.listing_key,
                        league_id=league.id,
                        player_id=player.id,
                        player_name=f"{player.first_name} {player.last_name}".strip()[:128],
                        first_seen=now,
                        last_seen=now,
                        expires_at=player.expires_at(market.fetched_at),
                        amount=player.my_offer,
                        asking_price=player.price,
                        market_value=player.market_value,
                    )
                )
                new += 1
            else:
                offer.amount = player.my_offer  # raised bids count with the last amount
                offer.last_seen = now

    async with sessions() as session:
        open_offers = list(
            (
                await session.execute(
                    select(MyOffer).where(MyOffer.league_id == league.id, MyOffer.outcome == "open")
                )
            ).scalars()
        )
    # Ended: past their end, gone from the market (bought, manager accepted) or withdrawn.
    ended = [
        o
        for o in open_offers
        if (o.expires_at is not None and o.expires_at <= now)
        or o.offer_id not in listed
        or not listed[o.offer_id].my_offer
    ]
    if not ended:
        return TrackResult(new, 0)
    squad = {p.id for p in (await client.squad(league.id, refresh=True)).data.players}
    evaluated = 0
    async with sessions.begin() as session:
        for offer in ended:
            purchase = await _purchase_after(session, offer)
            row = await session.get(MyOffer, offer.offer_id)
            if offer.player_id in squad and (purchase is None or purchase.by_me):
                row.outcome = "won"
                row.final_price = purchase.price if purchase else offer.amount
            elif purchase is not None and not purchase.by_me:
                row.outcome = "lost"
                row.final_price = purchase.price
            elif now - _end_time(offer) >= NO_SALE_AFTER:
                row.outcome = "none"
            else:
                continue  # transfers may still come in with the next feed run
            row.evaluated_at = now
            evaluated += 1
    return TrackResult(new, evaluated)


def _end_time(offer: MyOffer) -> datetime:
    """When the offer ended: its deadline, or (for endless manager listings) the last time it
    was still listed with an own bid."""
    return offer.expires_at if offer.expires_at is not None else offer.last_seen


async def _purchase_after(session, offer: MyOffer) -> LeagueTransfer | None:
    return (
        await session.execute(
            select(LeagueTransfer)
            .where(
                LeagueTransfer.league_id == offer.league_id,
                LeagueTransfer.player_id == offer.player_id,
                LeagueTransfer.kind == m.TransferKind.BOUGHT,
                LeagueTransfer.at >= offer.first_seen - MATCH_SLACK,
            )
            .order_by(LeagueTransfer.at)
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class OfferStats:
    won: int
    lost: int
    none: int
    open: int
    # Median of (winning price / own bid − 1) for lost bids: "lost by +x %".
    lost_gap: float | None
    # Median premium over the market value of won bids.
    won_premium: float | None
    recent: tuple[MyOffer, ...]

    @property
    def decided(self) -> int:
        return self.won + self.lost


async def offer_stats(sessions: async_sessionmaker, league_id: str, now: datetime) -> OfferStats:
    since = now - timedelta(days=STATS_DAYS)
    async with sessions() as session:
        offers = list(
            (
                await session.execute(
                    select(MyOffer)
                    .where(MyOffer.league_id == league_id, MyOffer.first_seen >= since)
                    .order_by(MyOffer.first_seen.desc())
                )
            ).scalars()
        )
    by = {o: [x for x in offers if x.outcome == o] for o in ("won", "lost", "none", "open")}
    gaps = [o.final_price / o.amount - 1 for o in by["lost"] if o.final_price and o.amount]
    premiums = [o.amount / o.market_value - 1 for o in by["won"] if o.amount and o.market_value]
    return OfferStats(
        won=len(by["won"]),
        lost=len(by["lost"]),
        none=len(by["none"]),
        open=len(by["open"]),
        lost_gap=statistics.median(gaps) if gaps else None,
        won_premium=statistics.median(premiums) if premiums else None,
        recent=tuple(offers[:10]),
    )
