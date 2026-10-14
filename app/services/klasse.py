"""Score component "Klasse": how good a player is beyond the last few matchdays.

Form only looks at the last matchdays, the trend only at the market value of the last week – so
a top player who is injured or has a quiet week scored like an average one. Klasse adds the
longer view:

- the market value's rank within the position (Kickbase prices years of performance in), and
- the season average (Kickbase points per appearance), whose share grows with the appearances
  up to `max_season_share` – early in the season a few matchdays say little.

Both as percentiles within the position (0 = worst, 1 = best), so "besser als x %" is literal.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MatchdayPoints, Player
from app.services.form import percentile_ranks
from app.web.formatting import money_short


@dataclass(frozen=True)
class ClassInput:
    value: float  # 0…1
    market_value: int
    mv_percentile: float
    average: float | None  # season points per appearance, None without appearances
    avg_percentile: float | None
    appearances: int
    season_share: float  # weight of the season average in `value`

    @property
    def reason(self) -> str:
        text = (
            f"Marktwert {money_short(self.market_value)} – besser als "
            f"{round(self.mv_percentile * 100)} % der Position"
        )
        if self.average is not None and self.avg_percentile is not None and self.season_share:
            text += (
                f"; Saison-Ø {self.average:.0f} in {self.appearances} Spielen – besser als "
                f"{round(self.avg_percentile * 100)} % (zählt {round(self.season_share * 100)} %)"
            )
        return text


async def load_class(
    sessions: async_sessionmaker,
    season: int | None,
    active_since: datetime,
    max_season_share: float,
    full_after_appearances: int,
) -> dict[str, ClassInput]:
    """Klasse per player id for every player updated since `active_since` (still in the
    Bundesliga)."""
    async with sessions() as session:
        players = (
            await session.execute(
                select(Player.id, Player.position, Player.market_value).where(
                    Player.updated_at >= active_since
                )
            )
        ).all()
        season_rows = []
        if season is not None:
            season_rows = (
                await session.execute(
                    select(
                        MatchdayPoints.player_id,
                        func.avg(MatchdayPoints.points),
                        func.count(),
                    )
                    .where(MatchdayPoints.season == season)
                    .group_by(MatchdayPoints.player_id)
                )
            ).all()
    seasons = {pid: (float(avg), int(n)) for pid, avg, n in season_rows}
    by_position: dict[int, list[tuple[str, int]]] = {}
    for pid, position, value in players:
        by_position.setdefault(position, []).append((pid, value))

    result: dict[str, ClassInput] = {}
    for members in by_position.values():
        mv_pct = percentile_ranks({pid: float(v) for pid, v in members})
        avg_pct = percentile_ranks({pid: seasons[pid][0] for pid, _ in members if pid in seasons})
        for pid, value in members:
            average, appearances = seasons.get(pid, (None, 0))
            share = max_season_share * min(appearances, full_after_appearances)
            share = share / full_after_appearances if full_after_appearances else 0.0
            if pid not in avg_pct:
                share = 0.0
            klass = (1 - share) * mv_pct[pid] + share * avg_pct.get(pid, 0.0)
            result[pid] = ClassInput(
                value=klass,
                market_value=value,
                mv_percentile=mv_pct[pid],
                average=average,
                avg_percentile=avg_pct.get(pid),
                appearances=appearances,
                season_share=share,
            )
    return result
