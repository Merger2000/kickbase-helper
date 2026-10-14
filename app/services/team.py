"""View model for the "Mein Team" page: header figures, pitch and squad list."""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.kickbase import models as m
from app.kickbase.client import Fetched, KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.services.lineup import Formation, SlotRange, infer_formation
from app.services.momentum import change_percent, daily_change

logger = logging.getLogger(__name__)

IMAGE_BASE_URL = "https://kickbase.b-cdn.net/"
LINE_ORDER = (m.Position.GOALKEEPER, m.Position.DEFENDER, m.Position.MIDFIELDER, m.Position.FORWARD)


def image_url(path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("https://") else IMAGE_BASE_URL + path.lstrip("/")


@dataclass(frozen=True)
class PlayerCard:
    id: str
    name: str
    position: m.Position
    team_id: str
    team_name: str | None
    image: str | None
    status: int
    market_value: int
    market_value_change_7d: int
    market_value_trend: m.MarketValueTrend
    average_points: int
    total_points: int
    lineup_slot: int | None = None
    opponent: str | None = None
    # False for players only known from My Eleven (squad data missing, e.g. caches out of sync).
    has_market_data: bool = True
    # Points of the last matchdays, oldest first (None = did not play).
    recent_points: tuple[int | None, ...] = ()
    market_value_change_24h: int | None = None

    @property
    def status_label(self) -> str:
        return m.status_label(self.status)

    @property
    def availability(self) -> m.Availability:
        return m.availability(self.status)

    @property
    def market_value_change_7d_percent(self) -> float | None:
        before = self.market_value - self.market_value_change_7d
        if before <= 0:
            return None
        return self.market_value_change_7d / before * 100

    @property
    def market_value_change_24h_percent(self) -> float | None:
        return change_percent(self.market_value, self.market_value_change_24h)

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper() or "?"


@dataclass(frozen=True)
class PitchLine:
    position: m.Position
    players: tuple[PlayerCard, ...]
    empty_slots: int


@dataclass(frozen=True)
class SquadGroup:
    position: m.Position
    players: tuple[PlayerCard, ...]


@dataclass(frozen=True)
class TeamOverview:
    league_name: str
    budget: int
    budget_after_offers: int
    team_value: int
    placement: int | None
    manager_count: int
    formation: Formation
    # Pitch lines from the forwards (top) down to the goalkeeper (bottom).
    lines: tuple[PitchLine, ...]
    bench: tuple[PlayerCard, ...]
    squad: tuple[SquadGroup, ...]
    squad_value: int
    fetched_at: datetime
    stale: bool

    @property
    def open_slots(self) -> int:
        return sum(line.empty_slots for line in self.lines)


async def load_team(
    client: KickbaseClient, league: ResolvedLeague, *, refresh: bool = False
) -> TeamOverview:
    # Sequential on purpose: the client sends one request at a time anyway, and most of these
    # are cache hits.
    me = await client.user_me()
    budget = await client.budget(league.id, refresh=refresh)
    ranking = await client.ranking(league.id, refresh=refresh)
    squad = await client.squad(league.id, refresh=refresh)
    lineup = await client.lineup(league.id, refresh=refresh)
    eleven = await client.my_eleven(league.id, refresh=refresh)
    table = await client.competition_table(league.competition_id)
    live: list[Fetched] = [budget, ranking, squad, lineup, eleven]

    team_names = {t.team_id: t.team_name for t in table.data.teams}
    lineup_by_id = {p.id: p for p in lineup.data.players}
    cards = [_card(p, lineup_by_id.get(p.id), team_names) for p in squad.data.players]

    known_ids = {c.id for c in cards}
    on_pitch_ids = {p.id for p in eleven.data.players}
    on_pitch = [c for c in cards if c.id in on_pitch_ids]
    missing = [p for p in eleven.data.players if p.id not in known_ids]
    if missing:
        # Never turn a lined-up player into a false "frei" slot.
        logger.warning("Aufgestellte Spieler fehlen im Kader: %s", [p.id for p in missing])
        on_pitch += [_card_from_eleven(p, lineup_by_id.get(p.id), team_names) for p in missing]
    bench = tuple(sorted((c for c in cards if c.id not in on_pitch_ids), key=_squad_order))
    formation, lines = _pitch(on_pitch)

    groups = tuple(
        SquadGroup(pos, tuple(sorted((c for c in cards if c.position is pos), key=_squad_order)))
        for pos in LINE_ORDER
    )
    squad_value = sum(c.market_value for c in cards)
    my_rank = ranking.data.for_user(me.data.user.id)
    if my_rank is None and ranking.data.users:
        logger.warning("Eigener Manager %s fehlt im Ranking", me.data.user.id)
    return TeamOverview(
        league_name=league.name,
        budget=budget.data.budget,
        budget_after_offers=budget.data.budget_after_offers,
        # Current sum of market values; the ranking's value is from the last matchday.
        team_value=squad_value,
        placement=my_rank.season_placement if my_rank else None,
        manager_count=len(ranking.data.users),
        formation=formation,
        lines=lines,
        bench=bench,
        squad=tuple(g for g in groups if g.players),
        squad_value=squad_value,
        # Age of the live data; user profile and table are cached much longer on purpose.
        fetched_at=min(f.fetched_at for f in live),
        stale=any(f.stale for f in (*live, me, table)),
    )


def _card(
    player: m.SquadPlayer, lineup: m.LineupPlayer | None, team_names: dict[str, str]
) -> PlayerCard:
    recent: tuple[int | None, ...] = ()
    if lineup is not None:
        recent = tuple(e.points if e.played else None for e in reversed(lineup.points_history))
    return PlayerCard(
        id=player.id,
        name=player.name,
        position=player.position,
        team_id=player.team_id,
        team_name=team_names.get(player.team_id),
        image=image_url(player.image),
        status=player.status,
        market_value=player.market_value,
        market_value_change_7d=player.market_value_change_7d,
        market_value_trend=player.market_value_trend,
        average_points=player.average_points,
        total_points=player.total_points,
        lineup_slot=lineup.lineup_slot if lineup is not None else player.lineup_slot,
        opponent=lineup.opponent_short_name if lineup is not None else None,
        recent_points=recent,
        market_value_change_24h=daily_change(player),
    )


def _card_from_eleven(
    player: m.MyElevenPlayer, lineup: m.LineupPlayer | None, team_names: dict[str, str]
) -> PlayerCard:
    return PlayerCard(
        id=player.id,
        name=player.name,
        position=player.position,
        team_id=player.team_id,
        team_name=team_names.get(player.team_id),
        image=image_url(player.image),
        status=player.status,
        market_value=0,
        market_value_change_7d=0,
        market_value_trend=m.MarketValueTrend.UNKNOWN,
        average_points=lineup.average_points if lineup else 0,
        total_points=0,
        lineup_slot=lineup.lineup_slot if lineup else None,
        has_market_data=False,
    )


def _squad_order(card: PlayerCard) -> tuple[int, int]:
    return (card.position.value, -card.market_value)


def _pitch(players: list[PlayerCard]) -> tuple[Formation, tuple[PitchLine, ...]]:
    by_line = {
        pos: sorted(
            (p for p in players if p.position is pos),
            key=lambda c: (c.lineup_slot is None, c.lineup_slot or 0, c.name),
        )
        for pos in LINE_ORDER
    }
    formation = infer_formation(
        len(by_line[m.Position.DEFENDER]),
        len(by_line[m.Position.MIDFIELDER]),
        len(by_line[m.Position.FORWARD]),
        defence=_slot_range(by_line[m.Position.DEFENDER]),
        midfield=_slot_range(by_line[m.Position.MIDFIELDER]),
        attack=_slot_range(by_line[m.Position.FORWARD]),
    )
    lines = tuple(
        PitchLine(
            position=pos,
            players=tuple(by_line[pos]),
            empty_slots=max(0, formation.slots(pos) - len(by_line[pos])),
        )
        for pos in reversed(LINE_ORDER)
    )
    return formation, lines


def _slot_range(players: list[PlayerCard]) -> SlotRange:
    slots = [p.lineup_slot for p in players if p.lineup_slot is not None]
    return SlotRange(min(slots), max(slots)) if slots else SlotRange()
