"""Observed market movements, not a forecast. Missing daily data stays unknown."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MarketValue


def change_percent(current: int, change: int | None) -> float | None:
    if change is None or current - change <= 0:
        return None
    return change / (current - change) * 100


def daily_change(player) -> int | None:
    """A model default of zero is not evidence of a flat market value."""
    if "market_value_change_24h" not in player.model_fields_set:
        return None
    return player.market_value_change_24h


@dataclass(frozen=True)
class Momentum:
    change_24h: int | None
    percent_24h: float | None
    change_3d: int | None = None
    percent_3d: float | None = None

    @property
    def rising(self) -> bool:
        return self.change_24h is not None and self.change_24h > 0


async def load_momentum(
    sessions: async_sessionmaker, values: dict[str, int], as_of: date
) -> dict[str, Momentum]:
    """Compare current values with exact market days; never call a multi-day gap '24 h'."""
    if not values:
        return {}
    days = {as_of - timedelta(days=1), as_of - timedelta(days=3)}
    async with sessions() as session:
        rows = (
            await session.execute(
                select(MarketValue.player_id, MarketValue.day, MarketValue.value).where(
                    MarketValue.player_id.in_(values), MarketValue.day.in_(days)
                )
            )
        ).all()
    old = {(pid, day): value for pid, day, value in rows}
    result = {}
    for pid, current in values.items():
        yesterday = old.get((pid, as_of - timedelta(days=1)))
        earlier = old.get((pid, as_of - timedelta(days=3)))
        delta = current - yesterday if yesterday is not None else None
        delta3 = current - earlier if earlier is not None else None
        result[pid] = Momentum(
            delta, change_percent(current, delta), delta3, change_percent(current, delta3)
        )
    return result
