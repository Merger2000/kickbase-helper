"""Player detail page and player search, combining the collected time series with live details."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import MarketValue, MatchdayPoints, MatchLineup, Player, Team
from app.db.stores import KeyValueStore
from app.kickbase import models as m
from app.kickbase.calendar import market_day
from app.kickbase.client import Fetched, KickbaseClient
from app.kickbase.league import ResolvedLeague
from app.lineups.service import (
    InjuryNote,
    Prognosis,
    injury_for_player,
    ligainsider_hint,
    predicted_players,
    prediction_for,
)
from app.services.momentum import daily_change
from app.services.scoring import load_scoring_config
from app.services.team import image_url
from app.services.transfers import BidAdvice, load_bid_status

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 50
# Use the collected market value series only if it is long and fresh enough.
MIN_SERIES_DAYS = 30
MAX_SERIES_AGE = timedelta(days=2)
# Players not updated by the daily sync for this long have left the Bundesliga.
DEPARTED_AFTER = timedelta(days=2)
# How many of the club's last matches (since the player's first appearance there) count for
# the start quote.
START_QUOTE_WINDOW = 5


@dataclass(frozen=True)
class StartQuote:
    started: int
    total: int


@dataclass(frozen=True)
class StartProbability:
    percent: int | None  # None: not enough data to say anything
    reasons: tuple[str, ...]


# Minimum of club matches (since his first appearance there) before the start quote is blended in.
MIN_QUOTE_MATCHES = 3


# LigaInsider entries that mean the player can't play at all.
OUT_KINDS = frozenset({"verletzung", "krankheit", "gesperrt", "nicht_im_kader"})
# Caps for players who may play, but not fully: never above a fit player in the same spot.
REHAB_CAP = 0.20
DOUBTFUL_CAP = 0.50


def compute_start_probability(
    prognosis: Prognosis,
    injury: InjuryNote | None,
    availability: m.Availability,
    start_quote: StartQuote | None,
    status_label: str = "verletzt/gesperrt",
) -> StartProbability:
    """Start probability 0…100 (or None without any data).

    1. Out (injured, sick, suspended, not in the squad) → 0.
    2. Otherwise the chance of a fit player: in the predicted eleven 85 %, position battle 50 %,
       not listed 15 %, blended with the historical start quote once enough club matches are
       known; without a (clear) prognosis the quote alone.
    3. Rehab caps that at 20 %, "angeschlagen" (or an unknown LigaInsider entry) at 50 % – a
       cap, not a replacement, so a limited player never ranks above a fit one.
    Every part is a reason."""
    if injury is not None and injury.kind in OUT_KINDS:
        return StartProbability(
            0, (injury.label + (f": {injury.reason}" if injury.reason else ""),)
        )
    if availability is m.Availability.OUT:
        return StartProbability(0, (status_label,))

    reasons: list[str] = []
    if prognosis.starting:
        base: float | None = 0.85
        reasons.append("voraussichtliche Startelf (LigaInsider)")
    elif prognosis.battle:
        base = 0.50
        reasons.append("Positionskampf um die Startelf (LigaInsider)")
    elif prognosis.starting is False:
        base = 0.15
        reasons.append("nicht in der voraussichtlichen Startelf")
    else:
        base = None
        reasons.append(
            f"Prognose unklar ({', '.join(prognosis.unmatched)} nicht zugeordnet)"
            if prognosis.unmatched
            else "keine Prognose"
        )
    if start_quote is not None and start_quote.total >= MIN_QUOTE_MATCHES:
        quote = start_quote.started / start_quote.total
        base = quote if base is None else 0.6 * base + 0.4 * quote
        reasons[-1] += f", Startquote {start_quote.started}/{start_quote.total}"

    cap, cap_reason = None, None
    if availability is m.Availability.REHAB or (
        injury is not None and injury.kind == "aufbautraining"
    ):
        cap, cap_reason = REHAB_CAP, "im Aufbautraining"
        if injury is not None and injury.reason:
            cap_reason += f": {injury.reason}"
    elif availability is m.Availability.DOUBTFUL:
        cap, cap_reason = DOUBTFUL_CAP, "angeschlagen (fraglich)"
    elif injury is not None:  # "angeschlagen" or an entry we can't classify: as doubtful
        cap, cap_reason = DOUBTFUL_CAP, f"{injury.label} (LigaInsider)"
    if cap is not None:
        reasons.insert(0, cap_reason)
        base = cap if base is None else min(base, cap)
    if base is None:
        return StartProbability(None, ("keine Prognose vorhanden",))
    return StartProbability(round(base * 100), tuple(reasons))


@dataclass(frozen=True)
class MatchdayBar:
    matchday: int
    points: int
    played: bool
    opponent: str | None
    is_home: bool | None


@dataclass(frozen=True)
class UpcomingMatch:
    matchday: int
    opponent: str | None
    is_home: bool
    kickoff: datetime


@dataclass(frozen=True)
class PlayerPage:
    id: str
    name: str
    team_name: str | None
    position: m.Position
    status: int
    image: str | None
    market_value: int
    market_value_change_7d: int | None
    average_points: int
    total_points: int
    goals: int
    assists: int
    market_values: tuple[tuple[date, int], ...]
    matchdays: tuple[MatchdayBar, ...]
    upcoming: tuple[UpcomingMatch, ...]
    season: int | None
    # True if the time series come from the collected data (otherwise live from Kickbase).
    collected: bool
    fetched_at: datetime
    stale: bool
    start_quote: StartQuote | None = None
    prognosis: Prognosis = field(default_factory=Prognosis)
    injury: InjuryNote | None = None
    # Why the start chance may be incomplete (outdated or unreadable LigaInsider data).
    prediction_hint: str | None = None
    team_id: str | None = None
    market_value_change_24h: int | None = None

    @property
    def status_label(self) -> str:
        return m.status_label(self.status)

    @property
    def availability(self) -> m.Availability:
        return m.availability(self.status)

    @property
    def start_probability(self) -> StartProbability:
        return compute_start_probability(
            self.prognosis, self.injury, self.availability, self.start_quote, self.status_label
        )

    @property
    def predicted_starting(self) -> bool | None:
        """True: in the predicted eleven; None: no, unclear or only a position battle."""
        return self.prognosis.starting

    @property
    def market_value_change_7d_percent(self) -> float | None:
        if self.market_value_change_7d is None:
            return None
        before = self.market_value - self.market_value_change_7d
        return self.market_value_change_7d / before * 100 if before > 0 else None

    @property
    def matches_played(self) -> int:
        return sum(1 for bar in self.matchdays if bar.played)

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper() or "?"

    @property
    def chart_data(self) -> dict:
        """Everything charts.js needs, embedded as JSON in the page."""
        return {
            "days": [day.isoformat() for day, _ in self.market_values],
            "values": [value for _, value in self.market_values],
            "matchdays": [bar.matchday for bar in self.matchdays],
            "points": [bar.points for bar in self.matchdays],
            "played": [bar.played for bar in self.matchdays],
            "opponents": [bar.opponent for bar in self.matchdays],
        }


async def load_player(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    player_id: str,
) -> PlayerPage:
    competition = league.competition_id
    details = await client.competition_player(competition, player_id)
    schedule = await client.competition_matchdays(competition)
    table = await client.competition_table(competition)
    fetched: list[Fetched] = [details, schedule, table]
    player = details.data
    season = schedule.data.season_start_year
    team_names = {t.team_id: t.team_name for t in table.data.teams}
    newest_allowed = market_day(details.fetched_at) - MAX_SERIES_AGE

    async with sessions() as session:
        values = [
            (row.day, row.value)
            for row in await session.scalars(
                select(MarketValue)
                .where(MarketValue.player_id == player_id)
                .order_by(MarketValue.day)
            )
        ]
        points = {
            row.matchday: row
            for row in await session.scalars(
                select(MatchdayPoints).where(
                    MatchdayPoints.player_id == player_id, MatchdayPoints.season == season
                )
            )
        }
        # Matchdays the worker has stored for the player's club: only there "no row" means
        # "not used"; elsewhere the data is simply not collected yet.
        synced = set(
            await session.scalars(
                select(MatchdayPoints.matchday)
                .where(MatchdayPoints.season == season, MatchdayPoints.team_id == player.team_id)
                .distinct()
            )
        )

    series_collected = len(values) >= MIN_SERIES_DAYS and values[-1][0] >= newest_allowed
    if not series_collected:
        history = await client.competition_player_market_value(competition, player_id, 92)
        fetched.append(history)
        values = [(point.day, point.value) for point in history.data.points]

    finished = [
        md.matchday
        for md in schedule.data.matchdays
        if md.matches and all(match.is_finished for match in md.matches)
    ]
    missing = [md for md in finished if md not in synced]
    live_bars: dict[int, MatchdayBar] = {}
    if missing and season is not None:
        performance = await client.competition_player_performance(competition, player_id)
        fetched.append(performance)
        live_bars = {
            bar.matchday: bar
            for bar in _bars_from_performance(performance.data, season, team_names)
        }
    bars = [
        live_bars[md] if md in live_bars else _bar_from_db(md, points.get(md), team_names)
        for md in finished
        if md in synced or md in live_bars
    ]

    reference_day = values[-1][0] if values else market_day(details.fetched_at)
    start_quote = await _start_quote(sessions, player_id, season) if season is not None else None
    now = details.fetched_at
    prognosis = prediction_for(
        await predicted_players(sessions, now, player.team_id), player.team_id, player.id
    )
    injury = await injury_for_player(sessions, player_id, now)
    prediction_hint = await ligainsider_hint(sessions, KeyValueStore(sessions), now)
    return PlayerPage(
        id=player.id,
        name=player.name,
        team_name=player.team_name or team_names.get(player.team_id),
        team_id=player.team_id,
        position=player.position,
        status=player.status,
        image=image_url(player.image),
        market_value=player.market_value,
        market_value_change_7d=_change_since(values, player.market_value, reference_day),
        market_value_change_24h=daily_change(player),
        average_points=player.average_points,
        total_points=player.total_points,
        goals=player.goals,
        assists=player.assists,
        market_values=tuple(values),
        matchdays=tuple(bars),
        upcoming=tuple(_upcoming(schedule.data, player.team_id, team_names)),
        season=season,
        collected=series_collected and not missing,
        fetched_at=min(f.fetched_at for f in fetched),
        stale=any(f.stale for f in fetched),
        start_quote=start_quote,
        prognosis=prognosis,
        injury=injury,
        prediction_hint=prediction_hint,
    )


def _bar_from_db(
    matchday: int, row: MatchdayPoints | None, team_names: dict[str, str]
) -> MatchdayBar:
    if row is None:
        return MatchdayBar(matchday, 0, False, None, None)
    return MatchdayBar(
        matchday, row.points, True, team_names.get(row.opponent_id or ""), row.is_home
    )


def _bars_from_performance(
    performance: m.Performance, season: int, team_names: dict[str, str]
) -> list[MatchdayBar]:
    title = f"{season}/{season + 1}"
    current = next((s for s in performance.seasons if s.title == title), None)
    if current is None:
        return []
    return [
        MatchdayBar(
            entry.matchday,
            entry.points or 0,
            entry.points is not None,
            team_names.get(entry.opponent_id or ""),
            entry.is_home,
        )
        for entry in current.matchdays
        if entry.is_finished
    ]


def _change_since(values: list[tuple[date, int]], current: int, latest_day: date) -> int | None:
    """Change against the value 7 days before the latest market day (2 days tolerance), so a
    gap in the data never turns a 30-day change into "7 Tage"."""
    target = latest_day - timedelta(days=7)
    candidates = [value for day, value in values if target - timedelta(days=2) <= day <= target]
    return current - candidates[-1] if candidates else None


def _upcoming(
    schedule: m.Matchdays, team_id: str, team_names: dict[str, str], limit: int = 3
) -> list[UpcomingMatch]:
    matches = []
    for md in schedule.matchdays:
        for match in md.matches:
            if match.is_finished or team_id not in (match.team1_id, match.team2_id):
                continue
            is_home = match.team1_id == team_id
            opponent = match.team2_id if is_home else match.team1_id
            matches.append(
                UpcomingMatch(md.matchday, team_names.get(opponent), is_home, match.kickoff)
            )
    return sorted(matches, key=lambda x: x.kickoff)[:limit]


def _quotes(rows) -> dict[str, StartQuote]:
    """Start quote per player over his club's last START_QUOTE_WINDOW matches.

    `rows` are (player_id, match_id, team_id, kickoff, started) of match_lineups. The club is
    the one of the player's latest appearance; a club match without a row for him (not in the
    matchday squad, e.g. injured or dropped) counts as not started. Matches before his first
    appearance for the club (a newcomer) don't count."""
    team_matches: dict[str, dict[str, datetime]] = {}
    per_player: dict[str, list] = {}
    for player_id, match_id, team_id, kickoff, started in rows:
        if kickoff is None:
            continue
        team_matches.setdefault(team_id, {})[match_id] = kickoff
        per_player.setdefault(player_id, []).append((kickoff, match_id, team_id, started))
    recent = {
        team: sorted(matches.items(), key=lambda item: item[1], reverse=True)
        for team, matches in team_matches.items()
    }
    quotes = {}
    for player_id, appearances in per_player.items():
        appearances.sort(reverse=True)
        team = appearances[0][2]
        at_club = [a for a in appearances if a[2] == team]
        first = at_club[-1][0]
        window = [mid for mid, kickoff in recent[team] if kickoff >= first][:START_QUOTE_WINDOW]
        started = {mid for _, mid, _, s in at_club if s}
        quotes[player_id] = StartQuote(sum(mid in started for mid in window), len(window))
    return quotes


_QUOTE_COLUMNS = (
    MatchLineup.player_id,
    MatchLineup.match_id,
    MatchLineup.team_id,
    MatchLineup.kickoff,
    MatchLineup.started,
)


async def _start_quote(
    sessions: async_sessionmaker, player_id: str, season: int
) -> StartQuote | None:
    """How often the player started in his club's last matches (see `_quotes`)."""
    async with sessions() as session:
        team = await session.scalar(
            select(MatchLineup.team_id)
            .where(MatchLineup.player_id == player_id, MatchLineup.season == season)
            .order_by(MatchLineup.kickoff.desc())
            .limit(1)
        )
        if team is None:
            return None
        rows = (
            await session.execute(
                select(*_QUOTE_COLUMNS).where(
                    MatchLineup.season == season, MatchLineup.team_id == team
                )
            )
        ).all()
    return _quotes(rows).get(player_id)


async def start_quotes_all(sessions: async_sessionmaker, season: int) -> dict[str, StartQuote]:
    """Start quote per player id for everyone with at least one squad appearance this season."""
    async with sessions() as session:
        rows = (
            await session.execute(select(*_QUOTE_COLUMNS).where(MatchLineup.season == season))
        ).all()
    return _quotes(rows)


@dataclass(frozen=True)
class MarketListing:
    price: int
    expires_at: datetime | None
    seller: str
    bid: BidAdvice | None
    bid_hint: str | None
    before_deadline: bool | None = None
    fetched_at: datetime | None = None
    stale: bool = False  # from the cache while Kickbase is unreachable: may be outdated


async def load_market_listing(
    client: KickbaseClient,
    sessions: async_sessionmaker,
    league: ResolvedLeague,
    player_id: str,
    now: datetime,
) -> MarketListing | None:
    """The player's market listing (asking price, deadline, seller) with the bid advice, or
    None when he is not on the market – like the buy page, own listings and offers that have
    ended (e.g. in a stale cache) don't count."""
    bids = await load_bid_status(sessions, league.id, now, load_scoring_config().bids)
    me = (await client.user_me()).data.user.id
    fetched = await client.market(league.id)
    deadline = fetched.data.deadline
    if deadline is not None and deadline <= now:
        deadline = None  # a deadline in the past says nothing about the next matchday
    for p in fetched.data.players:
        if p.id != player_id or (p.seller is not None and p.seller.id == me):
            continue
        expires_at = p.expires_at(fetched.fetched_at)
        if expires_at is not None and expires_at <= now:
            continue
        return MarketListing(
            price=p.price,
            expires_at=expires_at,
            seller=p.seller.name if p.seller else "Kickbase",
            bid=bids.model.advise(p.market_value, p.price),
            bid_hint=bids.hint,
            before_deadline=(
                expires_at <= deadline if expires_at is not None and deadline else None
            ),
            fetched_at=fetched.fetched_at,
            stale=fetched.stale,
        )
    return None


# ---------------------------------------------------------------------------- search


@dataclass(frozen=True)
class PlayerRow:
    id: str
    name: str
    team_name: str | None
    position: m.Position
    status: int
    market_value: int
    image: str | None

    @property
    def availability(self) -> m.Availability:
        return m.availability(self.status)

    @property
    def status_label(self) -> str:
        return m.status_label(self.status)

    @property
    def initials(self) -> str:
        return "".join(part[0] for part in self.name.split()[:2]).upper() or "?"


@dataclass(frozen=True)
class PlayerSearch:
    players: list[PlayerRow]
    # When the daily sync last updated the player data (None before the first sync).
    as_of: datetime | None


async def search_players(
    sessions: async_sessionmaker, query: str = "", position: int | None = None
) -> PlayerSearch:
    async with sessions() as session:
        as_of = await session.scalar(select(func.max(Player.updated_at)))
        if as_of is None:
            return PlayerSearch([], None)
        stmt = (
            select(Player, Team.name)
            .outerjoin(Team, Team.id == Player.team_id)
            # players the daily sync no longer sees have left the Bundesliga
            .where(Player.updated_at >= as_of - DEPARTED_AFTER)
            .order_by(Player.market_value.desc())
            .limit(SEARCH_LIMIT)
        )
        text = query.strip().replace("\\", "").replace("%", "").replace("_", "")
        if text:
            pattern = f"%{text}%"
            stmt = stmt.where(Player.name.ilike(pattern) | Team.name.ilike(pattern))
        if position:
            stmt = stmt.where(Player.position == position)
        rows = (await session.execute(stmt)).all()
    return PlayerSearch(
        [
            PlayerRow(
                id=player.id,
                name=player.name,
                team_name=team_name,
                position=m.Position(player.position),
                status=player.status,
                market_value=player.market_value,
                image=image_url(player.image),
            )
            for player, team_name in rows
        ],
        as_of,
    )
