"""Track record of the recommendations: were they any good?

Once a day every market offer and every squad player is logged with its score and whether it
was recommended (buy: in the default top list; sell: flagged). Later market values and points
show how recommended players did compared with the rest of the same day:

- buy: mean market value change after 1, 3 and 7 days, share that rose, points in the 7 days;
- sell: the same for flagged squad players (good when they fall compared with the others).

Only days whose horizon is complete count, and a horizon needs the market value of exactly that
day; nothing is interpolated.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MarketValue, MatchdayPoints, RecommendationLog
from app.db.upsert import upsert_many
from app.kickbase.calendar import market_day
from app.services.recommendations import BuyFilters, BuyView, Recommendation, SellView, select_buys

HORIZONS = (1, 3, 7)
POINTS_DAYS = 7
RECORD_DAYS = 60


def _components(rec: Recommendation) -> dict[str, int]:
    return dict(rec.score.shown_points)


async def log_recommendations(
    sessions: async_sessionmaker,
    league_id: str,
    now: datetime,
    buys: BuyView,
    sells: SellView,
) -> int:
    """Store today's snapshot (again on a second run the same day: replaced)."""
    day = market_day(now)
    recommended_buys = {r.player.id for r in select_buys(buys, BuyFilters())[0]}
    flagged = {r.player.id for r in sells.recommendations}
    rows = [
        {
            "league_id": league_id,
            "day": day,
            "view": view,
            "player_id": rec.player.id,
            "score": rec.score.total,
            "recommended": rec.player.id in chosen,
            "price": rec.price,
            "market_value": rec.market_value,
            "components": _components(rec),
        }
        for view, scored, chosen in (
            ("kaufen", buys.scored, recommended_buys),
            ("verkaufen", sells.scored, flagged),
        )
        for rec in scored
    ]
    async with sessions.begin() as session:
        await upsert_many(
            session, RecommendationLog, rows, keys=["league_id", "day", "view", "player_id"]
        )
    return len(rows)


@dataclass(frozen=True)
class Group:
    count: int
    mean_change: float | None  # mean relative market value change, e.g. 0.021 = +2,1 %
    rising: float | None  # share with a rising market value


@dataclass(frozen=True)
class HorizonRow:
    days: int
    recommended: Group
    others: Group

    @property
    def difference(self) -> float | None:
        a, b = self.recommended.mean_change, self.others.mean_change
        return None if a is None or b is None else a - b


@dataclass(frozen=True)
class PointsRow:
    recommended: float | None  # mean points in the 7 days after the snapshot
    others: float | None
    count: int


@dataclass(frozen=True)
class ViewRecord:
    view: str
    snapshots: int  # logged days
    horizons: tuple[HorizonRow, ...]
    points: PointsRow


@dataclass(frozen=True)
class TrackRecord:
    first_day: date | None
    views: dict[str, ViewRecord]

    @property
    def empty(self) -> bool:
        return all(r.snapshots == 0 for r in self.views.values())


def _group(changes: Iterable[float]) -> Group:
    values = list(changes)
    if not values:
        return Group(0, None, None)
    return Group(len(values), statistics.fmean(values), sum(v > 0 for v in values) / len(values))


async def track_record(sessions: async_sessionmaker, league_id: str, now: datetime) -> TrackRecord:
    today = market_day(now)
    since = today - timedelta(days=RECORD_DAYS)
    async with sessions() as session:
        logs = list(
            (
                await session.execute(
                    select(RecommendationLog).where(
                        RecommendationLog.league_id == league_id, RecommendationLog.day >= since
                    )
                )
            ).scalars()
        )
        players = {r.player_id for r in logs}
        values: dict[tuple[str, date], int] = {}
        if players:
            rows = await session.execute(
                select(MarketValue.player_id, MarketValue.day, MarketValue.value).where(
                    MarketValue.player_id.in_(players), MarketValue.day >= since
                )
            )
            values = {(p, d): v for p, d, v in rows}
            point_rows = await session.execute(
                select(MatchdayPoints.player_id, MatchdayPoints.kickoff, MatchdayPoints.points)
                .where(MatchdayPoints.player_id.in_(players))
                .where(MatchdayPoints.kickoff.is_not(None))
            )
            points = [(p, market_day(k), pts) for p, k, pts in point_rows]
        else:
            points = []
        first_day = await session.scalar(
            select(func.min(RecommendationLog.day)).where(RecommendationLog.league_id == league_id)
        )

    views = {}
    for view in ("kaufen", "verkaufen"):
        entries = [r for r in logs if r.view == view]
        horizons = []
        for days in HORIZONS:
            changes: dict[bool, list[float]] = {True: [], False: []}
            for r in entries:
                later = values.get((r.player_id, r.day + timedelta(days=days)))
                if r.day + timedelta(days=days) <= today and later and r.market_value:
                    changes[r.recommended].append(later / r.market_value - 1)
            horizons.append(HorizonRow(days, _group(changes[True]), _group(changes[False])))
        sums: dict[bool, list[int]] = {True: [], False: []}
        for r in entries:
            if r.day + timedelta(days=POINTS_DAYS) > today:
                continue
            total = sum(
                pts
                for p, d, pts in points
                if p == r.player_id and r.day < d <= r.day + timedelta(days=POINTS_DAYS)
            )
            sums[r.recommended].append(total)
        views[view] = ViewRecord(
            view=view,
            snapshots=len({r.day for r in entries}),
            horizons=tuple(horizons),
            points=PointsRow(
                recommended=statistics.fmean(sums[True]) if sums[True] else None,
                others=statistics.fmean(sums[False]) if sums[False] else None,
                count=len(sums[True]) + len(sums[False]),
            ),
        )
    return TrackRecord(first_day, views)
