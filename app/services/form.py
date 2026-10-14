"""Form of every player: recency-weighted points of the last matchdays, as a percentile within
the player's position."""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MatchdayPoints, Player
from app.services.players import DEPARTED_AFTER
from app.services.scoring import FormInput


@dataclass(frozen=True)
class FormTable:
    by_player: dict[str, FormInput]
    matchdays: tuple[int, ...]  # used for the form, newest first
    # Finished matchdays among the most recent ones that are not completely collected yet.
    incomplete: tuple[int, ...] = ()


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Share of the other players with a strictly lower value (0…1), so "besser als x %" is
    literally true; ties share the lower rank."""
    ordered = sorted(values.values())
    n = len(ordered)
    return {
        key: sum(1 for v in ordered if v < value) / (n - 1) if n > 1 else 0.5
        for key, value in values.items()
    }


async def load_form(
    sessions: async_sessionmaker,
    season: int,
    teams_per_matchday: dict[int, int],
    recency_weights: list[float],
) -> FormTable:
    """`teams_per_matchday`: finished matchdays and how many clubs played in them. A matchday
    only counts once points of all those clubs are stored (the worker saves match by match)."""
    async with sessions() as session:
        stored = dict(
            (
                await session.execute(
                    select(
                        MatchdayPoints.matchday, func.count(func.distinct(MatchdayPoints.team_id))
                    )
                    .where(MatchdayPoints.season == season)
                    .group_by(MatchdayPoints.matchday)
                )
            ).all()
        )
        newest = sorted(teams_per_matchday, reverse=True)
        complete = [md for md in newest if stored.get(md, 0) >= teams_per_matchday[md]]
        recent = complete[: len(recency_weights)]
        # Matchdays newer than the oldest one used but left out: the form lags behind them.
        cutoff = recent[-1] if recent else 0
        incomplete = tuple(md for md in newest if md > cutoff and md not in recent)
        as_of = await session.scalar(select(func.max(Player.updated_at)))
        if not recent or as_of is None:
            return FormTable({}, (), incomplete)
        players = (
            await session.execute(
                select(Player.id, Player.position).where(
                    Player.updated_at >= as_of - DEPARTED_AFTER
                )
            )
        ).all()
        rows = (
            await session.execute(
                select(
                    MatchdayPoints.player_id, MatchdayPoints.matchday, MatchdayPoints.points
                ).where(MatchdayPoints.season == season, MatchdayPoints.matchday.in_(recent))
            )
        ).all()

    points: dict[str, dict[int, int]] = defaultdict(dict)
    for player_id, matchday, value in rows:
        points[player_id][matchday] = value
    weights = recency_weights[: len(recent)]
    total_weight = sum(weights)

    weighted: dict[str, float] = {}
    by_position: dict[int, dict[str, float]] = defaultdict(dict)
    for player_id, position in players:
        played = points.get(player_id, {})
        value = sum(w * played.get(md, 0) for w, md in zip(weights, recent, strict=True))
        weighted[player_id] = value / total_weight
        by_position[position][player_id] = weighted[player_id]

    form = {}
    for group in by_position.values():
        for player_id, rank in percentile_ranks(group).items():
            form[player_id] = FormInput(
                percentile=rank,
                weighted_points=weighted[player_id],
                matchdays=len(recent),
                appearances=len(points.get(player_id, {})),
            )
    return FormTable(form, tuple(recent), incomplete)
