"""Per-matchday history for the team page (Block C, C3.4).

Pure DB access: reads `matchday_results` for the own league and returns a small
dataclass for the chart and the compact "letzte Bilanz" summary. No Kickbase
calls – the worker job in C3.3 fills the table."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MatchdayResult


@dataclass(frozen=True)
class MatchdayHistoryEntry:
    season: int
    matchday: int
    points: int
    placement: int
    matchday_placement: int | None
    team_value: int
    # Verpasste Punkte (sum of greedy same-position swap diffs). None on rows written before
    # the `missed_points` column existed (C3.2 / C3.3); the dashboard surfaces them only when
    # the value is known.
    missed_points: int | None


@dataclass(frozen=True)
class MatchdayHistory:
    entries: tuple[MatchdayHistoryEntry, ...]
    # Same as `entries[-1]` when present; kept separate so the template can
    # access "latest" without juggling with `entries|length`.
    latest: MatchdayHistoryEntry | None

    @property
    def empty(self) -> bool:
        return self.latest is None

    @property
    def latest_placement_delta(self) -> int | None:
        """Positive = moved up. None on the first stored matchday or when the
        previous matchday is not in the history (e.g. only days 1 and 3 are
        stored after a backfill gap) – the delta would compare unrelated rows.
        """
        if self.latest is None or len(self.entries) < 2:
            return None
        if self.entries[-2].matchday != self.latest.matchday - 1:
            return None
        return self.entries[-2].placement - self.latest.placement

    def chart_payload(self) -> dict[str, Any]:
        """JSON-friendly shape for `charts.js` (matches the player-page convention
        of one `<script type="application/json">` element per chart)."""
        return {
            "matchdays": [e.matchday for e in self.entries],
            "placements": [e.placement for e in self.entries],
        }

    @property
    def chart_data(self) -> dict[str, Any]:
        return self.chart_payload()


async def load_matchday_history(sessions: async_sessionmaker, league_id: str) -> MatchdayHistory:
    """All stored rows for the own league, ordered by matchday ascending.

    The current season only – Kickbase resets the league each summer, and mixing
    seasons would draw a meaningless line. With one row per matchday the table
    is tiny, so we read everything and let the caller drop what it doesn't need."""
    async with sessions() as session:
        latest_season = await session.scalar(
            select(MatchdayResult.season)
            .where(MatchdayResult.league_id == league_id)
            .order_by(MatchdayResult.season.desc())
            .limit(1)
        )
        if latest_season is None:
            return MatchdayHistory(entries=(), latest=None)
        rows = (
            await session.execute(
                select(MatchdayResult)
                .where(
                    MatchdayResult.league_id == league_id,
                    MatchdayResult.season == latest_season,
                )
                .order_by(MatchdayResult.matchday)
            )
        ).scalars()
        entries = tuple(
            MatchdayHistoryEntry(
                season=row.season,
                matchday=row.matchday,
                points=row.points,
                placement=row.placement,
                matchday_placement=row.matchday_placement,
                team_value=row.team_value,
                missed_points=row.missed_points,
            )
            for row in rows
        )
    return MatchdayHistory(entries=entries, latest=entries[-1] if entries else None)
