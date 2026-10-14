"""League transfers and the bid recommendation built from them.

Transfers come from the league's activity feed (type 15, newest first, 50 per page). For every
purchase the premium is compared with the player's market value on that market day:
premium = price / market value − 1. The recommended bid for a listing is its market value plus
the chosen quantile of the premiums paid for comparable players (same price class), e.g. "higher
than 70 % of the premiums paid for 23 comparable purchases in the last 60 days".

We only see the winning prices, not the bids that lost, so this is an estimate of what it takes
to win, and the page says so.
"""

import logging
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ApiCache, JobRun, LeagueTransfer, MarketValue
from app.kickbase import models as m
from app.kickbase.calendar import market_day
from app.kickbase.client import KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.services.scoring import BidConfig

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_PAGES = 20  # safety net for the first backfill (about 1000 transfers)
# The market value of the purchase day; when it is missing, an earlier day within this range.
VALUE_LOOKBACK_DAYS = 3
ROUND_TO = 1_000  # round up to whole thousands: amount and premium stay consistent


JOB_NAME = "collect_league_transfers"
# The job runs every two hours; older than this means it stopped working.
JOB_MAX_AGE = timedelta(hours=6)


@dataclass(frozen=True)
class CollectResult:
    new: int
    pages: int
    valued: int
    unreadable: int = 0


async def collect_transfers(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    my_name: str,
    now: datetime,
    period_days: int,
) -> CollectResult:
    """Page through the feed until a known transfer, the end of the feed or `period_days`;
    then fill missing market values. Buyer names are compared with `my_name` and dropped."""
    oldest = now - timedelta(days=period_days)
    await backfill_sellers_from_cache(sessions, league.id, my_name)
    new_rows: dict[str, LeagueTransfer] = {}
    pages = unreadable = 0
    for page in range(MAX_PAGES):
        feed = (
            await client.transfers(league.id, start=page * PAGE_SIZE, limit=PAGE_SIZE, refresh=True)
        ).data
        pages += 1
        await identify_sellers(sessions, league.id, feed.items, my_name)
        activities = [a for a in feed.items if a.type == m.TRANSFER_ACTIVITY]
        ids = [a.id for a in activities]
        async with sessions() as session:
            known = set(
                (
                    await session.execute(
                        select(LeagueTransfer.activity_id).where(
                            LeagueTransfer.activity_id.in_(ids)
                        )
                    )
                ).scalars()
            )
        for activity in activities:
            if activity.id in known or activity.id in new_rows or activity.at < oldest:
                continue
            try:
                transfer = activity.transfer()
            except ValidationError as exc:
                # Only where it failed: the raw data may contain a manager's name.
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
                logger.warning("Transfer %s nicht lesbar, übersprungen (%s)", activity.id, problems)
                unreadable += 1
                continue
            new_rows[activity.id] = LeagueTransfer(
                activity_id=activity.id,
                league_id=league.id,
                at=activity.at,
                player_id=transfer.player_id,
                player_name=transfer.player_name[:128],
                team_id=transfer.team_id,
                kind=transfer.kind,
                price=transfer.price,
                by_me=bool(my_name and transfer.buyer and transfer.buyer == my_name),
                sold_by_me=transfer.seller == my_name if transfer.seller and my_name else None,
            )
        reached_known = bool(known)
        reached_end = len(feed.items) < PAGE_SIZE
        reached_oldest = any(a.at < oldest for a in feed.items)
        if reached_known or reached_end or reached_oldest:
            break
    else:
        logger.warning("Transfer-Feed: nach %d Seiten abgebrochen", MAX_PAGES)
    if new_rows:
        async with sessions.begin() as session:
            session.add_all(new_rows.values())
    valued = await fill_market_values(sessions, league.id)
    return CollectResult(len(new_rows), pages, valued, unreadable)


async def identify_sellers(
    sessions: async_sessionmaker, league_id: str, activities: list[m.Activity], my_name: str
) -> int:
    """Add only missing identity flags for exactly matching existing transfers. No names saved."""
    if not my_name:
        return 0
    parsed = {}
    conflicting = set()
    for activity in activities:
        if activity.type != m.TRANSFER_ACTIVITY:
            continue
        try:
            transfer = activity.transfer()
        except ValidationError:
            continue
        if transfer.seller:
            if activity.id in parsed and parsed[activity.id][1].seller != transfer.seller:
                conflicting.add(activity.id)
            parsed[activity.id] = (activity, transfer)
    parsed = {aid: value for aid, value in parsed.items() if aid not in conflicting}
    if not parsed:
        return 0
    changed = 0
    async with sessions.begin() as session:
        rows = await session.scalars(
            select(LeagueTransfer).where(
                LeagueTransfer.league_id == league_id,
                LeagueTransfer.activity_id.in_(parsed),
                LeagueTransfer.sold_by_me.is_(None),
            )
        )
        for row in rows:
            activity, transfer = parsed[row.activity_id]
            if (row.at, row.player_id, row.kind, row.price) != (
                activity.at,
                transfer.player_id,
                transfer.kind,
                transfer.price,
            ):
                continue
            row.sold_by_me = transfer.seller == my_name
            changed += 1
    return changed


async def backfill_sellers_from_cache(
    sessions: async_sessionmaker, league_id: str, my_name: str
) -> int:
    """Recover historical flags from saved pages; no API requests or invented ownership."""
    async with sessions() as session:
        payloads = (
            await session.scalars(
                select(ApiCache.payload).where(
                    ApiCache.key.startswith(f"/v4/leagues/{league_id}/activitiesFeed?")
                )
            )
        ).all()
    activities = []
    for payload in payloads:
        try:
            activities.extend(m.ActivityFeed.model_validate(payload).items)
        except ValidationError:
            continue
    return await identify_sellers(sessions, league_id, activities, my_name)


async def fill_market_values(sessions: async_sessionmaker, league_id: str) -> int:
    """Market value on the purchase's market day where still missing. An earlier day (up to 3
    back) only stands in once that day can no longer arrive, i.e. a later day is synced already;
    until then the row waits, so a premium is never computed against yesterday's value."""
    async with sessions() as session:
        newest_synced = await session.scalar(select(func.max(MarketValue.day)))
        open_rows = list(
            (
                await session.execute(
                    select(LeagueTransfer).where(
                        LeagueTransfer.league_id == league_id,
                        LeagueTransfer.market_value.is_(None),
                    )
                )
            ).scalars()
        )
        if not open_rows:
            return 0
        days = {r.activity_id: market_day(r.at) for r in open_rows}
        earliest = min(days.values()) - timedelta(days=VALUE_LOOKBACK_DAYS)
        values = await session.execute(
            select(MarketValue.player_id, MarketValue.day, MarketValue.value).where(
                MarketValue.player_id.in_({r.player_id for r in open_rows}),
                MarketValue.day >= earliest,
            )
        )
        by_player: dict[str, dict] = {}
        for player_id, day, value in values:
            by_player.setdefault(player_id, {})[day] = value
    filled = {}
    for row in open_rows:
        known = by_player.get(row.player_id, {})
        day = days[row.activity_id]
        settled = newest_synced is not None and day < newest_synced
        for back in range(VALUE_LOOKBACK_DAYS + 1 if settled else 1):
            value = known.get(day - timedelta(days=back))
            if value:
                filled[row.activity_id] = value
                break
    if filled:
        async with sessions.begin() as session:
            for activity_id, value in filled.items():
                row = await session.get(LeagueTransfer, activity_id)
                row.market_value = value
    return len(filled)


# ---------------------------------------------------------------------------- bid model


def quantile(sorted_values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (like numpy's default)."""
    if not sorted_values:
        raise ValueError("keine Werte")
    position = (len(sorted_values) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


@dataclass(frozen=True)
class BidAdvice:
    amount: int
    premium: float  # of `amount` over the market value, e.g. 0.09 for +9 %
    model_premium: float  # the quantile of the comparable premiums
    at_asking_price: bool  # the asking price is above the model's bid: bid the asking price
    comparable: int
    same_class: bool  # False: too few in the price class, all classes were used
    median_premium: float
    high_premium: float  # 90 % quantile: a bid that very likely wins
    quantile: float
    period_days: int

    @property
    def quantile_percent(self) -> int:
        return round(self.quantile * 100)


@dataclass(frozen=True)
class BidModel:
    config: BidConfig
    # Premiums per price class (index into price_bounds + 1), each sorted.
    by_class: tuple[tuple[float, ...], ...]

    @property
    def all_premiums(self) -> list[float]:
        return sorted(p for premiums in self.by_class for p in premiums)

    def price_class(self, market_value: int) -> int:
        return bisect_right(self.config.price_bounds, market_value)

    def advise(self, market_value: int, price: int) -> BidAdvice | None:
        if market_value <= 0:
            return None
        premiums = list(self.by_class[self.price_class(market_value)])
        same_class = len(premiums) >= self.config.min_comparable
        if not same_class:
            premiums = self.all_premiums
            if len(premiums) < self.config.min_comparable:
                return None
        q = quantile(premiums, self.config.quantile)
        model_amount = math.ceil(market_value * (1 + q) / ROUND_TO) * ROUND_TO
        amount = max(model_amount, price)
        return BidAdvice(
            amount=amount,
            premium=amount / market_value - 1,
            model_premium=q,
            at_asking_price=price > model_amount,
            comparable=len(premiums),
            same_class=same_class,
            median_premium=quantile(premiums, 0.5),
            high_premium=quantile(premiums, 0.9),
            quantile=self.config.quantile,
            period_days=self.config.period_days,
        )


async def load_bid_model(
    sessions: async_sessionmaker, league_id: str, now: datetime, config: BidConfig
) -> BidModel:
    """Premiums of all purchases by managers in the period with a known market value."""
    since = now - timedelta(days=config.period_days)
    async with sessions() as session:
        rows = await session.execute(
            select(LeagueTransfer.price, LeagueTransfer.market_value).where(
                LeagueTransfer.league_id == league_id,
                LeagueTransfer.kind == m.TransferKind.BOUGHT,
                LeagueTransfer.at >= since,
                LeagueTransfer.market_value > 0,
            )
        )
        classes: list[list[float]] = [[] for _ in range(len(config.price_bounds) + 1)]
        for price, value in rows:
            classes[bisect_right(config.price_bounds, value)].append(price / value - 1)
    return BidModel(config, tuple(tuple(sorted(c)) for c in classes))


@dataclass(frozen=True)
class BidStatus:
    model: BidModel
    usable: int  # purchases with market value in the period
    without_value: int  # purchases in the period still waiting for their market value
    hint: str | None  # why bids may be missing or not current, for the page


async def load_bid_status(
    sessions: async_sessionmaker, league_id: str, now: datetime, config: BidConfig
) -> BidStatus:
    """The bid model plus an honest note when it can't (fully) be trusted."""
    model = await load_bid_model(sessions, league_id, now, config)
    since = now - timedelta(days=config.period_days)
    async with sessions() as session:
        without_value = await session.scalar(
            select(func.count())
            .select_from(LeagueTransfer)
            .where(
                LeagueTransfer.league_id == league_id,
                LeagueTransfer.kind == m.TransferKind.BOUGHT,
                LeagueTransfer.at >= since,
                LeagueTransfer.market_value.is_(None),
            )
        )
        last_ok = await session.scalar(
            select(func.max(JobRun.finished_at)).where(
                JobRun.job == JOB_NAME, JobRun.status == "ok"
            )
        )
    usable = len(model.all_premiums)
    hints = []
    if last_ok is None:
        hints.append("Die Liga-Transfers wurden noch nicht gesammelt.")
    elif now - last_ok > JOB_MAX_AGE:
        hints.append(
            "Die Liga-Transfers wurden seit über 6 Stunden nicht aktualisiert (Worker-Job prüfen)."
        )
    if usable < config.min_comparable:
        waiting = f", {without_value} warten noch auf ihren Marktwert" if without_value else ""
        hints.append(
            f"Gebotsempfehlung ab {config.min_comparable} Käufen mit Marktwert; "
            f"bisher {usable}{waiting}."
        )
    return BidStatus(model, usable, without_value, " ".join(hints) or None)
