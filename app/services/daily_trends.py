"""Confirmed consecutive market-day changes; no forecasts or extrapolation across gaps."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MarketValue
from app.kickbase.calendar import market_day, near_market_value_update
from app.kickbase.client import Fetched
from app.kickbase.models import Squad
from app.services.momentum import daily_change

HISTORY_DAYS = 7


@dataclass(frozen=True)
class DailyMovement:
    day: date
    value: int | None
    change: int | None


@dataclass(frozen=True)
class DailyTrend:
    player_id: str
    name: str
    movements: tuple[DailyMovement, ...]
    confirmed: bool

    @property
    def latest(self) -> int | None:
        return self.movements[-1].change

    @property
    def turn(self) -> str | None:
        if not self.confirmed:
            return None
        previous, current = self.movements[-2].change, self.latest
        if previous is not None and current is not None:
            if previous > 0 > current:
                return "down"
            if previous < 0 < current:
                return "up"
        return None

    @property
    def streak(self) -> int:
        if not self.confirmed or self.latest in (None, 0):
            return 0
        count = 0
        for movement in reversed(self.movements):
            delta = movement.change
            if delta is None or delta * self.latest <= 0:
                break
            count += 1
        return count

    @property
    def label(self) -> str:
        if not self.confirmed:
            return "Aktueller Tagesstand nicht bestätigt"
        if self.turn == "down":
            return "Trendwende nach unten"
        if self.turn == "up":
            return "Trendwende nach oben"
        if self.latest == 0:
            return "Letztes Update unverändert"
        updates = "Update" if self.streak == 1 else "Updates"
        return f"{self.streak} {updates} {'gestiegen' if self.latest > 0 else 'gefallen'}"


@dataclass(frozen=True)
class DailyTrends:
    as_of: date
    players: tuple[DailyTrend, ...]
    fetched_at: datetime
    stale: bool

    @property
    def turns(self) -> tuple[DailyTrend, ...]:
        return tuple(p for p in self.players if p.turn is not None)

    @property
    def unconfirmed(self) -> int:
        return sum(not p.confirmed for p in self.players)

    @property
    def others(self) -> tuple[DailyTrend, ...]:
        return tuple(p for p in self.players if p.turn is None)


def build_trends(
    squad: Fetched[Squad], values: dict[tuple[str, date], int], now: datetime
) -> DailyTrends:
    as_of = market_day(now)
    age = now - squad.fetched_at
    stable = (
        not squad.stale
        and timedelta(0) <= age < timedelta(minutes=10)
        and market_day(squad.fetched_at) == as_of
    )
    stable = (
        stable
        and not near_market_value_update(now)
        and not near_market_value_update(squad.fetched_at)
    )
    result = []
    for player in squad.data.players:
        days = []
        for back in reversed(range(HISTORY_DAYS)):
            day = as_of - timedelta(days=back)
            current, previous = (
                values.get((player.id, day)),
                values.get((player.id, day - timedelta(days=1))),
            )
            delta = (
                current - previous
                if current is not None and previous is not None and current > 0 and previous > 0
                else None
            )
            days.append(DailyMovement(day, current, delta))
        latest = days[-1]
        confirmed = (
            stable
            and latest.change is not None
            and latest.value == player.market_value
            and latest.change == daily_change(player)
        )
        result.append(DailyTrend(player.id, player.name, tuple(days), confirmed))
    result.sort(
        key=lambda p: (p.turn is None, p.turn != "down", not p.confirmed, p.name.casefold())
    )
    return DailyTrends(as_of, tuple(result), squad.fetched_at, squad.stale)


async def load_trends(
    sessions: async_sessionmaker, squad: Fetched[Squad], now: datetime
) -> DailyTrends:
    ids = [p.id for p in squad.data.players]
    as_of = market_day(now)
    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    select(MarketValue.player_id, MarketValue.day, MarketValue.value).where(
                        MarketValue.player_id.in_(ids),
                        MarketValue.day >= as_of - timedelta(days=HISTORY_DAYS),
                        MarketValue.day <= as_of,
                    )
                )
            ).all()
            if ids
            else []
        )
    return build_trends(squad, {(pid, day): value for pid, day, value in rows}, now)
