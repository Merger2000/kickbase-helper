"""Pydantic models for Kickbase API responses.

Field names are descriptive; the aliases are the short keys of the API. The spec documents almost
no response schemas, so these models were derived from real (anonymised) responses in
`tests/fixtures/kickbase/`. Unknown keys are ignored; only fields we use are modelled.
"""

from datetime import UTC, date, datetime, timedelta
from enum import Enum, IntEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

EPOCH = date(1970, 1, 1)


def _round_money(value: object) -> object:
    # Amounts sometimes arrive as floats (17012409.0); a fractional part must not fail a list.
    return round(value) if isinstance(value, float) else value


def _to_str(value: object) -> object:
    # The user teamcenter returns identifiers as ints (tid, mi); everywhere else they are
    # strings. Coerce so a single model can parse both shapes.
    return str(value) if value is not None else None


def _assume_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


Money = Annotated[int, BeforeValidator(_round_money)]
# Identifiers that may arrive as int or str; coerce for a stable type downstream.
IdStr = Annotated[str, BeforeValidator(_to_str)]
UtcDatetime = Annotated[datetime, AfterValidator(_assume_utc)]


class KickbaseModel(BaseModel):
    # hide_input_in_errors: raw data may contain other managers' names (e.g. transfer buyers).
    model_config = ConfigDict(
        populate_by_name=True, extra="ignore", frozen=True, hide_input_in_errors=True
    )


class Position(IntEnum):
    GOALKEEPER = 1
    DEFENDER = 2
    MIDFIELDER = 3
    FORWARD = 4

    @property
    def short_label(self) -> str:
        return {1: "TW", 2: "ABW", 3: "MF", 4: "ST"}[self.value]


class MarketValueTrend(IntEnum):
    """`mvt`: direction of the last market value change."""

    UNKNOWN = -1
    UNCHANGED = 0
    RISING = 1
    FALLING = 2

    @classmethod
    def _missing_(cls, value: object) -> "MarketValueTrend":
        return cls.UNKNOWN  # an unexpected code must not fail a whole squad or market list


# Player status (`st`). Codes 0, 1, 2 and 4 occur in the fixtures; the labels follow the
# Kickbase app and community documentation and are not part of the official spec.
STATUS_LABELS: dict[int, str] = {
    0: "fit",
    1: "verletzt",
    2: "angeschlagen",
    4: "Aufbautraining",
    8: "Rot-Sperre",
    16: "Gelb-Rot-Sperre",
    32: "Gelbsperre",
    64: "nicht im Kader",
    128: "nicht in der Liga",
    256: "abwesend",
}


def status_label(code: int) -> str:
    return STATUS_LABELS.get(code, f"Status {code}")


class Availability(Enum):
    """Coarse player availability, shared by UI badges and the scoring status malus."""

    FIT = "fit"
    DOUBTFUL = "fraglich"
    REHAB = "Aufbautraining"
    OUT = "fällt aus"
    UNKNOWN = "unbekannt"


def availability(code: int) -> Availability:
    if code == 0:
        return Availability.FIT
    if code == 2:
        return Availability.DOUBTFUL
    if code == 4:
        return Availability.REHAB
    if code in STATUS_LABELS:
        return Availability.OUT
    return Availability.UNKNOWN


def _finished(match_status: int | None) -> bool:
    # Match status codes seen in responses: 0 = scheduled, 2 = finished.
    return match_status == 2


# ---------------------------------------------------------------------------- user & leagues


class UserProfile(KickbaseModel):
    id: str
    name: str


class UserMe(KickbaseModel):
    """`GET /v4/user/me` (e-mail and other account details are deliberately not modelled)."""

    user: UserProfile = Field(alias="u")


class LeagueSummary(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")
    competition_id: str = Field(alias="cpi")
    budget: Money | None = Field(default=None, alias="b")
    team_value: Money | None = Field(default=None, alias="tv")
    placement: int | None = Field(default=None, alias="pl")


class LeagueSelection(KickbaseModel):
    """`GET /v4/leagues/selection`: all leagues of the account."""

    leagues: list[LeagueSummary] = Field(alias="it")


class LeagueMe(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/me`"""

    budget: Money = Field(alias="b")
    league_name: str = Field(alias="lnm")
    competition_id: str = Field(alias="cpi")
    is_admin: bool = Field(default=False, alias="adm")


class Budget(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/me/budget`"""

    budget: Money = Field(alias="b")
    # Budget minus all of the manager's pending offers; only sent while offers are open.
    pending_offers_budget: Money | None = Field(default=None, alias="pbaa")
    budget_status: int | None = Field(default=None, alias="bs")

    @property
    def budget_after_offers(self) -> int:
        return self.budget if self.pending_offers_budget is None else self.pending_offers_budget


# ---------------------------------------------------------------------------- players (shared)


class PointsEntry(KickbaseModel):
    """One entry of `ph`, most recent matchday first. It carries no matchday number (a postponed
    match shifts positions), so store points per matchday from `PerformanceEntry` instead."""

    played: bool = Field(alias="hp")
    points: int = Field(default=0, alias="p")


class SquadPlayer(KickbaseModel):
    """Item of `GET /v4/leagues/{leagueId}/squad`"""

    id: str = Field(alias="i")
    name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    market_value: Money = Field(alias="mv")
    market_value_trend: MarketValueTrend = Field(alias="mvt")
    market_value_change_24h: Money = Field(default=0, alias="tfhmvt")
    market_value_change_7d: Money = Field(default=0, alias="sdmvt")
    # Market value change since the manager bought the player.
    market_value_gain: Money = Field(default=0, alias="mvgl")
    total_points: int = Field(default=0, alias="p")
    average_points: int = Field(default=0, alias="ap")
    lineup_slot: int | None = Field(default=None, alias="lo")
    offer_count: int = Field(default=0, alias="ofc")
    image: str | None = Field(default=None, alias="pim")

    @property
    def status_label(self) -> str:
        return status_label(self.status)

    @property
    def availability(self) -> Availability:
        return availability(self.status)


class Squad(KickbaseModel):
    players: list[SquadPlayer] = Field(alias="it")


class LineupPlayer(KickbaseModel):
    """Item of `GET /v4/leagues/{leagueId}/lineup`"""

    id: str = Field(alias="i")
    name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    average_points: int = Field(default=0, alias="ap")
    lineup_slot: int | None = Field(default=None, alias="lo")
    opponent_short_name: str | None = Field(default=None, alias="os")
    points_history: list[PointsEntry] = Field(default_factory=list, alias="ph")
    image: str | None = Field(default=None, alias="pim")


class Lineup(KickbaseModel):
    players: list[LineupPlayer] = Field(alias="it")


class OpponentTeam(KickbaseModel):
    team_id: str = Field(alias="i")
    image: str | None = Field(default=None, alias="tim")


class MyElevenPlayer(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    match_id: str | None = Field(default=None, alias="mi")
    kickoff: UtcDatetime | None = Field(default=None, alias="md")
    match_status: int | None = Field(default=None, alias="mst")
    opponent: OpponentTeam | None = Field(default=None, alias="ot")
    image: str | None = Field(default=None, alias="pim")


class MyEleven(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/teamcenter/myeleven`"""

    players: list[MyElevenPlayer] = Field(alias="lp")
    bench: list[MyElevenPlayer] = Field(default_factory=list, alias="nlp")
    points: int = Field(default=0, alias="p")
    lineup_player_count: int = Field(default=0, alias="lpc")


# ---------------------------------------------------------------------------- ranking


class RankingUser(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")
    season_points: int = Field(default=0, alias="sp")
    matchday_points: int = Field(default=0, alias="mdp")
    team_value: Money = Field(default=0, alias="tv")
    season_placement: int = Field(alias="spl")
    matchday_placement: int | None = Field(default=None, alias="mdpl")


class Ranking(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/ranking`"""

    league_name: str = Field(alias="ti")
    # Latest matchday with points (4 while matchday 5 is upcoming).
    latest_matchday: int | None = Field(default=None, alias="day")
    users: list[RankingUser] = Field(alias="us")

    def for_user(self, user_id: str) -> RankingUser | None:
        return next((u for u in self.users if u.id == user_id), None)


# ---------------------------------------------------------------------------- transfer market


class MarketSeller(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")


class MarketOffer(KickbaseModel):
    """One entry of `MarketPlayer.offers`. `unm` is deliberately not modelled: bidder names
    are another manager's private data and must never reach logs, alerts or the LLM."""

    amount: Money = Field(alias="uop")
    # `uoid`/`u` both carry the bidder id (anonymised in fixtures); accept whichever the
    # response uses today.
    bidder_id: str = Field(validation_alias=AliasChoices("uoid", "u"))
    status: int = Field(alias="st")


class MarketPlayer(KickbaseModel):
    """Item of `GET /v4/leagues/{leagueId}/market`. Without `seller` the player is sold by
    Kickbase itself."""

    id: str = Field(alias="i")
    first_name: str = Field(default="", alias="fn")
    last_name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    market_value: Money = Field(alias="mv")
    market_value_trend: MarketValueTrend = Field(alias="mvt")
    price: Money = Field(alias="prc")
    expires_in_seconds: int | None = Field(default=None, alias="exs")
    listed_at: UtcDatetime | None = Field(default=None, alias="dt")
    total_points: int = Field(default=0, alias="p")
    average_points: int = Field(default=0, alias="ap")
    seller: MarketSeller | None = Field(default=None, alias="u")
    my_offer: Money | None = Field(default=None, alias="uop")
    offer_count: int = Field(default=0, alias="ofc")
    offers: list[MarketOffer] = Field(default_factory=list, alias="ofs")
    image: str | None = Field(default=None, alias="pim")

    @property
    def listing_key(self) -> str:
        """Identifies this listing (a player can be listed again later). `uoid` is not an offer
        id but the bidder's user id, so the listing time is used."""
        stamp = int(self.listed_at.timestamp()) if self.listed_at else 0
        return f"{self.id}@{stamp}"

    @property
    def best_offer(self) -> MarketOffer | None:
        """Highest open bid on this listing, or `None` when nobody has bid yet."""
        return max(self.offers, key=lambda o: o.amount, default=None)

    def expires_at(self, fetched_at: datetime) -> datetime | None:
        """`exs` counts from the moment the market was fetched (see `Fetched.fetched_at`)."""
        if self.expires_in_seconds is None:
            return None
        return fetched_at + timedelta(seconds=self.expires_in_seconds)


class Market(KickbaseModel):
    players: list[MarketPlayer] = Field(alias="it")
    # Next matchday to be played (5 while matchday 4 is the latest with points).
    next_matchday: int | None = Field(default=None, alias="day")
    next_market_value_update: UtcDatetime | None = Field(default=None, alias="mvud")
    # Lineup deadline of the next matchday (e.g. Friday 18:30 UTC): players bought later miss it.
    deadline: UtcDatetime | None = Field(default=None, alias="dt")


# ---------------------------------------------------------------------------- activities

TRANSFER_ACTIVITY = 15  # `filter` value and activity type of transfers


class TransferKind(IntEnum):
    BOUGHT = 1  # bought on the market by a manager (`byr`)
    SOLD_TO_KICKBASE = 2  # sold directly to Kickbase at market value


class Transfer(KickbaseModel):
    """`data` of a transfer activity (type 15)."""

    player_id: str = Field(alias="pi")
    player_name: str = Field(default="", alias="pn")
    team_id: str | None = Field(default=None, alias="tid")
    price: Money = Field(alias="trp")
    kind: int = Field(alias="t")
    buyer: str | None = Field(default=None, alias="byr")  # a manager name: never store or log
    seller: str | None = Field(default=None, alias="slr")  # verified in saved activity fixture


class Activity(KickbaseModel):
    id: Annotated[str, BeforeValidator(str)] = Field(alias="i")
    type: int = Field(alias="t")
    at: UtcDatetime = Field(alias="dt")
    data: dict = Field(default_factory=dict)

    def transfer(self) -> Transfer | None:
        return Transfer.model_validate(self.data) if self.type == TRANSFER_ACTIVITY else None


class ActivityFeed(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/activitiesFeed`: newest first, paged with start/max."""

    items: list[Activity] = Field(alias="af")


# ---------------------------------------------------------------------------- player details


class MatchSummary(KickbaseModel):
    team1_id: str = Field(alias="t1")
    team2_id: str = Field(alias="t2")
    team1_goals: int | None = Field(default=None, alias="t1g")
    team2_goals: int | None = Field(default=None, alias="t2g")
    matchday: int = Field(alias="day")
    kickoff: UtcDatetime | None = Field(default=None, alias="md")
    is_current: bool = Field(default=False, alias="cur")
    match_status: int | None = Field(default=None, alias="mdst")

    @property
    def is_finished(self) -> bool:
        return _finished(self.match_status)


class PlayerDetails(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/players/{playerId}` and
    `GET /v4/competitions/{competitionId}/players/{playerId}`"""

    id: str = Field(alias="i")
    first_name: str = Field(default="", alias="fn")
    last_name: str = Field(alias="ln")
    shirt_number: int | None = Field(default=None, alias="shn")
    team_id: str = Field(alias="tid")
    team_name: str | None = Field(default=None, alias="tn")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    status_list: list[int] = Field(default_factory=list, alias="stl")
    total_points: int = Field(default=0, alias="tp")
    average_points: int = Field(default=0, alias="ap")
    seconds_played: int = Field(default=0, alias="sec")
    goals: int = Field(default=0, alias="g")
    assists: int = Field(default=0, alias="a")
    yellow_cards: int = Field(default=0, alias="y")
    red_cards: int = Field(default=0, alias="r")
    market_value: Money = Field(alias="mv")
    market_value_trend: MarketValueTrend = Field(alias="mvt")
    market_value_change_24h: Money = Field(default=0, alias="tfhmvt")
    points_history: list[PointsEntry] = Field(default_factory=list, alias="ph")
    latest_matchday: int | None = Field(default=None, alias="day")
    matches: list[MatchSummary] = Field(default_factory=list, alias="mdsum")
    # Manager owning the player in this league; None when nobody owns him (API sends 0).
    owner_id: str | None = Field(default=None, alias="oui")
    image: str | None = Field(default=None, alias="pim")
    team_image: str | None = Field(default=None, alias="tim")

    @field_validator("owner_id", mode="before")
    @classmethod
    def _no_owner(cls, value: object) -> object:
        return None if value in (0, "0", "", None) else value

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def status_label(self) -> str:
        return status_label(self.status)

    @property
    def availability(self) -> Availability:
        return availability(self.status)


class PerformanceEntry(KickbaseModel):
    matchday: int = Field(alias="day")
    points: int | None = Field(default=None, alias="p")
    minutes_played: int | None = Field(default=None, alias="mp")
    kickoff: UtcDatetime | None = Field(default=None, alias="md")
    team1_id: str | None = Field(default=None, alias="t1")
    team2_id: str | None = Field(default=None, alias="t2")
    team1_goals: int | None = Field(default=None, alias="t1g")
    team2_goals: int | None = Field(default=None, alias="t2g")
    player_team_id: str | None = Field(default=None, alias="pt")
    # `st` here is not the player status (other codes); deliberately not modelled.
    is_current: bool = Field(default=False, alias="cur")
    match_status: int | None = Field(default=None, alias="mdst")

    @property
    def is_finished(self) -> bool:
        return _finished(self.match_status)

    @field_validator("minutes_played", mode="before")
    @classmethod
    def _parse_minutes(cls, value: object) -> object:
        # The API sends minutes as text, e.g. "76'" or "90+3'".
        if isinstance(value, str):
            head = value.strip().rstrip("'").split("+")[0]
            return int(head) if head.isdigit() else None
        return value

    @property
    def is_home(self) -> bool | None:
        if self.player_team_id is None:
            return None
        return self.player_team_id == self.team1_id

    @property
    def opponent_id(self) -> str | None:
        if self.player_team_id is None:
            return None
        return self.team2_id if self.is_home else self.team1_id


class PerformanceSeason(KickbaseModel):
    season_id: str | None = Field(default=None, alias="sid")
    title: str = Field(alias="ti")
    matchdays: list[PerformanceEntry] = Field(default_factory=list, alias="ph")


class Performance(KickbaseModel):
    """`.../players/{playerId}/performance`: per-matchday data for several seasons."""

    seasons: list[PerformanceSeason] = Field(alias="it")


class MarketValuePoint(KickbaseModel):
    days_since_epoch: int = Field(alias="dt")
    value: Money = Field(alias="mv")

    @property
    def day(self) -> date:
        return EPOCH + timedelta(days=self.days_since_epoch)


class MarketValueHistory(KickbaseModel):
    """`.../players/{playerId}/marketValue/{timeframe}` (timeframe 92 or 365 days)."""

    points: list[MarketValuePoint] = Field(alias="it")
    lowest: Money | None = Field(default=None, alias="lmv")
    highest: Money | None = Field(default=None, alias="hmv")


# ---------------------------------------------------------------------------- competition


class Competition(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")


class Competitions(KickbaseModel):
    competitions: list[Competition] = Field(alias="it")


class CompetitionPlayer(KickbaseModel):
    """Item of `GET /v4/competitions/{competitionId}/players`: a top list (25 entries), not
    the full player pool. Its `st` has a different meaning than the player status."""

    id: str = Field(alias="pi")
    name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    points: int = Field(default=0, alias="p")
    goals: int = Field(default=0, alias="g")
    assists: int = Field(default=0, alias="a")
    image: str | None = Field(default=None, alias="pim")


class CompetitionPlayers(KickbaseModel):
    players: list[CompetitionPlayer] = Field(alias="it")
    latest_matchday: int | None = Field(default=None, alias="day")


class TableEntry(KickbaseModel):
    team_id: str = Field(alias="tid")
    team_name: str = Field(alias="tn")
    points: int = Field(alias="cp")
    placement: int = Field(alias="cpl")
    previous_placement: int | None = Field(default=None, alias="pcpl")
    matches_played: int = Field(alias="mc")
    goal_difference: int = Field(alias="gd")
    kickbase_points: int | None = Field(default=None, alias="sp")
    image: str | None = Field(default=None, alias="tim")


class CompetitionTable(KickbaseModel):
    teams: list[TableEntry] = Field(alias="it")


class Match(KickbaseModel):
    id: str = Field(alias="mi")
    matchday: int = Field(alias="day")
    kickoff: UtcDatetime = Field(alias="dt")
    team1_id: str = Field(alias="t1")
    team2_id: str = Field(alias="t2")
    team1_short_name: str | None = Field(default=None, alias="t1sy")
    team2_short_name: str | None = Field(default=None, alias="t2sy")
    team1_goals: int | None = Field(default=None, alias="t1g")
    team2_goals: int | None = Field(default=None, alias="t2g")
    status: int | None = Field(default=None, alias="st")

    @property
    def is_finished(self) -> bool:
        return _finished(self.status)


class MatchPlayer(KickbaseModel):
    """A player in a match's lineup (starting eleven or bench)."""

    id: Annotated[str, BeforeValidator(str)] = Field(alias="i")
    name: str = Field(default="", alias="n")
    position: Position = Field(alias="pos")


class MatchDetails(KickbaseModel):
    """`GET /v4/matches/{matchId}/details`: lineups and status of one match."""

    team1_id: str = Field(alias="t1")
    team2_id: str = Field(alias="t2")
    status: int | None = Field(default=None, alias="mst")
    kickoff: UtcDatetime | None = Field(default=None, alias="md")
    team1_starting: list[MatchPlayer] = Field(default_factory=list, alias="t1lp")
    team1_bench: list[MatchPlayer] = Field(default_factory=list, alias="t1nlp")
    team2_starting: list[MatchPlayer] = Field(default_factory=list, alias="t2lp")
    team2_bench: list[MatchPlayer] = Field(default_factory=list, alias="t2nlp")


class Matchday(KickbaseModel):
    matchday: int = Field(alias="day")
    matches: list[Match] = Field(alias="it")


class Matchdays(KickbaseModel):
    """`GET /v4/competitions/{competitionId}/matchdays`: the full season schedule."""

    matchdays: list[Matchday] = Field(alias="it")
    # Matchday in progress or next to be played.
    next_matchday: int | None = Field(default=None, alias="day")

    @property
    def season_start_year(self) -> int | None:
        """2026 for the season 2026/27 (seasons start in summer); None without a schedule."""
        kickoffs = [match.kickoff for md in self.matchdays for match in md.matches]
        if not kickoffs:
            return None
        first = min(kickoffs)
        return first.year if first.month >= 7 else first.year - 1


class TeamProfilePlayer(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")
    team_id: str = Field(alias="tid")
    position: Position = Field(alias="pos")
    status: int = Field(alias="st")
    market_value: Money = Field(alias="mv")
    market_value_trend: MarketValueTrend = Field(alias="mvt")
    market_value_change_7d: Money = Field(default=0, alias="sdmvt")
    image: str | None = Field(default=None, alias="pim")


class TeamProfile(KickbaseModel):
    """`GET /v4/competitions/{competitionId}/teams/{teamId}/teamprofile`: all players of a club
    with market value and status (but no points)."""

    team_id: str = Field(alias="tid")
    team_name: str = Field(alias="tn")
    placement: int | None = Field(default=None, alias="pl")
    team_value: Money | None = Field(default=None, alias="tv")
    wins: int | None = Field(default=None, alias="tw")
    draws: int | None = Field(default=None, alias="td")
    losses: int | None = Field(default=None, alias="tl")
    players: list[TeamProfilePlayer] = Field(alias="it")
    image: str | None = Field(default=None, alias="tim")


class TeamCenterPlayer(KickbaseModel):
    id: str = Field(alias="i")
    name: str = Field(alias="n")
    points: int = Field(default=0, alias="p")
    image: str | None = Field(default=None, alias="pim")


class TeamCenterMatch(KickbaseModel):
    id: str = Field(alias="mi")
    kickoff: UtcDatetime | None = Field(default=None, alias="dt")
    team1_id: str = Field(alias="t1")
    team2_id: str = Field(alias="t2")
    team1_goals: int | None = Field(default=None, alias="t1g")
    team2_goals: int | None = Field(default=None, alias="t2g")
    status: int | None = Field(default=None, alias="st")

    @property
    def is_finished(self) -> bool:
        return _finished(self.status)


class TeamCenter(KickbaseModel):
    """`GET /v4/competitions/{competitionId}/teams/{teamId}/teamcenter?dayNumber=N`: the club's
    match of that matchday and the points of every player who was used (others scored 0)."""

    team_id: str = Field(alias="tid")
    team_name: str = Field(alias="tn")
    match: TeamCenterMatch | None = Field(default=None, alias="ma")
    players: list[TeamCenterPlayer] = Field(default_factory=list, alias="it")


# ---------------------------------------------------------------------------- user teamcenter


class UserTeamCenterPlayer(KickbaseModel):
    """Item in the requester's `lp`/`nlp` of `UserTeamCenter`: one player with the points he
    scored this matchday and the match context. `st` is a kicker participation code (3 = 90+ min,
    5 = subbed in or out), not the squad's player status."""

    id: IdStr = Field(alias="i")
    last_name: str = Field(alias="n")
    team_id: IdStr = Field(alias="tid")
    points: int = Field(default=0, alias="p")
    participation: int = Field(default=0, alias="st")
    match_id: IdStr | None = Field(default=None, alias="mi")
    minutes_total: int | None = Field(default=None, alias="mt")
    minutes_detail: str | None = Field(default=None, alias="mtd")
    kickoff: UtcDatetime | None = Field(default=None, alias="md")
    match_status: int | None = Field(default=None, alias="mst")
    kicker_events: list[int] = Field(default_factory=list, alias="k")
    image: str | None = Field(default=None, alias="pim")
    in_current_team_position: bool = Field(default=False, alias="ictp")

    @property
    def name(self) -> str:
        return self.last_name

    @property
    def played(self) -> bool:
        # `mst` 2 = finished; 0/None = not yet played (future matchday).
        return self.match_status == 2


class UserTeamCenterLineupItem(KickbaseModel):
    """Item in `UserTeamCenterUser.lpi`: just the player's id, image and a flag – the other
    managers' lineups never include points."""

    id: IdStr = Field(alias="i")
    image: str | None = Field(default=None, alias="pim")
    in_current_team_position: bool = Field(default=False, alias="ictp")


class UserTeamCenterUser(KickbaseModel):
    """One entry of `UserTeamCenter.us` – all managers' matchday lineups (just ids and images).
    The requester appears here too, with `i` matching the queried user id; richer data lives
    on `UserTeamCenter.lineup` / `.bench`."""

    id: IdStr = Field(alias="i")
    name: str = Field(alias="unm")
    matchday_points: int = Field(default=0, alias="mdp")
    placement: int = Field(default=0, alias="pl")
    played_all: bool = Field(default=False, alias="pa")
    lineup_player_ids: list[IdStr] = Field(default_factory=list, alias="lp")
    lineup_player_items: list[UserTeamCenterLineupItem] = Field(default_factory=list, alias="lpi")
    image: str | None = Field(default=None, alias="uim")


class UserTeamCenter(KickbaseModel):
    """`GET /v4/leagues/{leagueId}/users/{userId}/teamcenter?dayNumber=N`: the requester's
    lineup (with points) and the other managers' lineups (just ids) for one matchday."""

    # Own user fields. The endpoint returns `n` (the manager name) and `uim` directly on the
    # top level; they are the requester's own data, anonymised in fixtures like everything else.
    name: str = Field(alias="n")
    lineup: list[UserTeamCenterPlayer] = Field(default_factory=list, alias="lp")
    bench: list[UserTeamCenterPlayer] = Field(default_factory=list, alias="nlp")
    image: str | None = Field(default=None, alias="uim")
    ppc: int = Field(default=0, alias="ppc")
    clpc: int = Field(default=0, alias="clpc")
    users: list[UserTeamCenterUser] = Field(alias="us")
