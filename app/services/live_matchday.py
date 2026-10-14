"""Own frozen matchday lineup and provisional Kickbase points; no current-squad fallback."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.kickbase import models as m
from app.kickbase.client import Fetched, KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.services.team import image_url


@dataclass(frozen=True)
class LivePlayer:
    id: str
    name: str
    points: int | None
    image: str | None
    kickoff: datetime | None
    match_status: int | None

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper() or "?"

    @property
    def status_label(self) -> str:
        # Only code 2 is verified. In particular, do not guess a meaning for mst=1.
        if self.match_status == 2:
            return "Abgepfiffen"
        return "Status offen"


@dataclass(frozen=True)
class LiveMatchday:
    matchday: int
    lineup: tuple[LivePlayer, ...]
    bench: tuple[LivePlayer, ...]
    official_points: int | None
    lineup_points: int | None
    fetched_at: datetime
    stale: bool
    now: datetime
    auto_refresh: bool
    interval: int

    @property
    def open_slots(self) -> int:
        return max(0, 11 - len(self.lineup))

    @property
    def differs(self) -> bool:
        return (
            self.official_points is not None
            and self.lineup_points is not None
            and self.official_points != self.lineup_points
        )


def started_matchday(schedule: m.Matchdays, now: datetime) -> m.Matchday | None:
    """Latest *started* round from the schedule, not `day` (often next week's round).

    A postponed match from an older round does not switch the screen away from the newest.
    """
    started = [
        day
        for day in schedule.matchdays
        if day.matches and min(match.kickoff for match in day.matches) <= now
    ]
    return max(started, key=lambda day: min(m.kickoff for m in day.matches), default=None)


def _player(player: m.UserTeamCenterPlayer) -> LivePlayer:
    return LivePlayer(
        player.id,
        player.name,
        player.points if "points" in player.model_fields_set else None,
        image_url(player.image),
        player.kickoff,
        player.match_status,
    )


def build_live_matchday(
    fetched: Fetched[m.UserTeamCenter],
    schedule: Fetched[m.Matchdays],
    day: m.Matchday,
    user_id: str,
    now: datetime,
    interval: int = 600,
) -> LiveMatchday:
    own = next((u for u in fetched.data.users if u.id == user_id), None)
    official = (
        own.matchday_points
        if own is not None and "matchday_points" in own.model_fields_set
        else None
    )
    lineup = tuple(_player(p) for p in fetched.data.lineup)
    bench = tuple(_player(p) for p in fetched.data.bench)
    total = (
        sum(p.points for p in lineup)
        if lineup and all(p.points is not None for p in lineup)
        else None
    )
    # Poll only while a scheduled game could currently be running (unknown codes stay unknown).
    active = any(
        not match.is_finished and match.kickoff <= now < match.kickoff + timedelta(hours=6)
        for match in day.matches
    )
    return LiveMatchday(
        day.matchday,
        lineup,
        bench,
        official,
        total,
        fetched.fetched_at,
        fetched.stale or schedule.stale,
        now,
        active,
        interval,
    )


async def load_live_matchday(
    client: KickbaseClient,
    league: ResolvedLeague,
    now: datetime | None = None,
) -> LiveMatchday | None:
    now = now or datetime.now(UTC)
    schedule = await client.competition_matchdays(league.competition_id)
    day = started_matchday(schedule.data, now)
    if day is None:
        return None
    user = (await client.user_me()).data.user
    fetched = await client.user_teamcenter(league.id, user.id, day.matchday, live=True)
    return build_live_matchday(fetched, schedule, day, user.id, now, max(600, client.ttl.live))
