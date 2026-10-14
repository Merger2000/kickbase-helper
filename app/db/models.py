from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names so Alembic migrations stay reproducible.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops the offset otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ApiCache(Base):
    """Cached API responses, shared by app and worker."""

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class AuthToken(Base):
    """API tokens (never logged), so app, worker and restarts reuse one login."""

    __tablename__ = "auth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class KeyValue(Base):
    """Small pieces of state, e.g. the resolved league id."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------------------- Bundesliga data


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # Kickbase team id
    name: Mapped[str] = mapped_column(String(64))
    image: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Player(Base):
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # Kickbase player id
    name: Mapped[str] = mapped_column(String(128))
    team_id: Mapped[str] = mapped_column(String(16), index=True)
    position: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[int] = mapped_column(Integer)
    market_value: Mapped[int] = mapped_column(BigInteger)
    image: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MarketValue(Base):
    """One market value per player and day (daily job plus a one-off backfill)."""

    __tablename__ = "market_values"

    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (Index("ix_market_values_day", "day"),)


class PlayerStatsDaily(Base):
    """Daily snapshot of status and club, e.g. to see when an injury started."""

    __tablename__ = "player_stats_daily"

    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[str] = mapped_column(String(16))
    market_value_change_7d: Mapped[int | None] = mapped_column(BigInteger)


class MatchdayPoints(Base):
    """Kickbase points of a player in one matchday. Players without a row did not play."""

    __tablename__ = "matchday_points"

    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)  # start year, 2026/27
    matchday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    points: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[str] = mapped_column(String(16))
    opponent_id: Mapped[str | None] = mapped_column(String(16))
    is_home: Mapped[bool | None] = mapped_column(Boolean)
    kickoff: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (Index("ix_matchday_points_player", "player_id", "season"),)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(16))  # running / ok / failed
    requests: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------- language model


class AiUsage(Base):
    """One row per request to the language model (also failed ones): the basis of the hard
    limits in `config/ai.yaml`, shared by app and worker."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    task: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16)
    )  # ok / rate_limited / error / bad_output / quota
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------- news


class NewsItem(Base):
    """A news article from a feed (kicker RSS); `processed_at` is set once the language model
    has extracted the reports about absences, or after too many failed attempts."""

    __tablename__ = "news_items"

    guid: Mapped[str] = mapped_column(String(512), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    teaser: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(String(1024))
    categories: Mapped[Any] = mapped_column(JSONType)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    reprocess_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notification_handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NewsReport(Base):
    """An injury, suspension or return mentioned in a news item, matched to a Kickbase player
    where the name and club are unambiguous."""

    __tablename__ = "news_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    news_guid: Mapped[str] = mapped_column(String(512), index=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    player_name: Mapped[str] = mapped_column(String(128))
    team_id: Mapped[str | None] = mapped_column(String(16))
    player_id: Mapped[str | None] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # verletzung / krankheit / sperre / ...
    status: Mapped[str] = mapped_column(String(24))  # faellt_aus / fraglich / ...
    absence: Mapped[str | None] = mapped_column(Text)
    expected_return: Mapped[str | None] = mapped_column(Text)
    # Set once the Telegram step handled the report (sent, or not about an own player).
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    team_name: Mapped[str | None] = mapped_column(String(128))
    source_excerpt: Mapped[str | None] = mapped_column(String(280))


# ---------------------------------------------------------------------------- league transfers


class LeagueTransfer(Base):
    """A transfer in the league (activity feed, type 15). Manager names are never stored, only
    own buyer/seller flags (None for unidentified sellers). `market_value` is that market day's,
    filled from `market_values` once known."""

    __tablename__ = "league_transfers"

    activity_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    league_id: Mapped[str] = mapped_column(String(16), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    player_id: Mapped[str] = mapped_column(String(16), index=True)
    player_name: Mapped[str] = mapped_column(String(128))
    team_id: Mapped[str | None] = mapped_column(String(16))
    kind: Mapped[int] = mapped_column(SmallInteger)  # 1 bought by a manager, 2 sold to Kickbase
    price: Mapped[int] = mapped_column(BigInteger)
    by_me: Mapped[bool] = mapped_column(Boolean, default=False)
    sold_by_me: Mapped[bool | None] = mapped_column(Boolean)  # None: seller not identified
    market_value: Mapped[int | None] = mapped_column(BigInteger)


# ---------------------------------------------------------------- own bids and track record


class MyOffer(Base):
    """An own bid seen on the market (`uoid`/`uop`) and, after the offer ended, its outcome."""

    __tablename__ = "my_offers"

    offer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    league_id: Mapped[str] = mapped_column(String(16), index=True)
    player_id: Mapped[str] = mapped_column(String(16))
    player_name: Mapped[str] = mapped_column(String(128))
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime())  # last run it was still listed
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # None: manager listing
    amount: Mapped[int] = mapped_column(BigInteger)
    asking_price: Mapped[int] = mapped_column(BigInteger)
    market_value: Mapped[int] = mapped_column(BigInteger)
    # open / won / lost / none (ended without a sale, or withdrawn)
    outcome: Mapped[str] = mapped_column(String(8), default="open", server_default="open")
    final_price: Mapped[int | None] = mapped_column(BigInteger)  # paid by the winner
    evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RecommendationLog(Base):
    """Daily snapshot of every market offer and squad player with its score and whether it was
    recommended; the track record compares later market values and points."""

    __tablename__ = "recommendation_log"
    __table_args__ = (
        UniqueConstraint("league_id", "day", "view", "player_id"),
        Index("ix_recommendation_log_day", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[str] = mapped_column(String(16))
    day: Mapped[date] = mapped_column(Date)  # market day of the snapshot
    view: Mapped[str] = mapped_column(String(16))  # kaufen / verkaufen
    player_id: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    recommended: Mapped[bool] = mapped_column(Boolean)
    price: Mapped[int | None] = mapped_column(BigInteger)
    market_value: Mapped[int] = mapped_column(BigInteger)
    components: Mapped[Any] = mapped_column(JSONType)  # shown points per score component


class MatchLineup(Base):
    """Whether a player was in a match's starting eleven (bench included; players not in the
    matchday squad have no row). Phase 7: the basis for the start quote."""

    __tablename__ = "match_lineups"
    __table_args__ = (Index("ix_match_lineups_player", "player_id", "season"),)

    match_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    season: Mapped[int] = mapped_column(SmallInteger)
    matchday: Mapped[int] = mapped_column(SmallInteger)
    team_id: Mapped[str] = mapped_column(String(16))
    started: Mapped[bool] = mapped_column(Boolean)
    kickoff: Mapped[datetime | None] = mapped_column(UTCDateTime())


class LineupPrediction(Base):
    """A predicted starting eleven from a scraped source (LigaInsider, Phase 7). Replaced on
    every fetch; `player_id` stays None when the name can't be matched unambiguously."""

    __tablename__ = "lineup_predictions"
    __table_args__ = (Index("ix_lineup_predictions_team", "team_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    team_id: Mapped[str] = mapped_column(String(16))
    player_id: Mapped[str | None] = mapped_column(String(16))
    player_name: Mapped[str] = mapped_column(String(128))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # startelf, or alternative (the other candidate in a position battle)
    status: Mapped[str] = mapped_column(String(16), default="startelf", server_default="startelf")


class Injury(Base):
    """An injury/suspension from a scraped source (LigaInsider, Phase 7). Replaced on every
    fetch; `player_id` stays None when the name can't be matched unambiguously."""

    __tablename__ = "injuries"
    __table_args__ = (Index("ix_injuries_player", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    team_id: Mapped[str] = mapped_column(String(16))
    player_id: Mapped[str | None] = mapped_column(String(16))
    player_name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(24))  # verletzung / gesperrt / ...
    reason: Mapped[str | None] = mapped_column(Text)
    since: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------------------- watchlist


class Watchlist(Base):
    """A player the user wants to keep an eye on (Block C, C1).

    `price_below` is an optional alarm threshold in Kickbase money (so 1_000_000 = 1 Mio).
    `(league_id, player_id)` is the primary key, so the same player cannot be added twice in
    one league and adding an existing row is a no-op (idempotent)."""

    __tablename__ = "watchlist"

    league_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime())
    # None = no price alarm, just the on-market/status/lineup notifications.
    price_below: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MatchdayResult(Base):
    """Per-matchday summary of the own team (Block C, C3): points, placements and team value.

    `(league_id, season, matchday)` is the primary key – exactly one row per matchday for the
    own user. Written after the matchday finishes and the Telegram alarm has been sent (or
    silently dropped when disabled). `missed_points` is the sum of bench-vs-lineup gaps the
    greedy swap in `app.services.matchday_report` would have closed (0 when no swap helps);
    populated for rows written after the column was added, NULL on older rows."""

    __tablename__ = "matchday_results"

    league_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    season: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    matchday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # Sum of points from the own lineup (Kickbase's `p` on the teamcenter).
    points: Mapped[int] = mapped_column(Integer)
    # Season placement after this matchday (= ranking's `spl` for that day).
    placement: Mapped[int] = mapped_column(Integer)
    # Placement of this single matchday (= ranking's `mdpl`); None for not played.
    matchday_placement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sum of market values of the squad (Kickbase ranking's `tv`).
    team_value: Mapped[int] = mapped_column(BigInteger)
    # Total points left on the bench (greedy same-position swap); None for older rows.
    missed_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class HoldingRole(Base):
    """Own local roster preference; independent of Kickbase orders."""

    __tablename__ = "holding_roles"
    __table_args__ = (CheckConstraint("role IN ('core', 'trade')", name="valid_role"),)
    league_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    role: Mapped[str] = mapped_column(String(5))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime())
