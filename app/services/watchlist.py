"""Watchlist service (Block C, C1): CRUD for the players the user wants to keep an eye on.

The primary key is `(league_id, player_id)`, so adding the same player twice is a no-op,
removing a missing one returns `False`, and a price threshold (`None` to clear) is stored
alongside. The service refuses to add an unknown `player_id` so a typo from the UI cannot
silently hide a real player.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Player, Watchlist
from app.kickbase.models import Availability


async def watched_ids(sessions: async_sessionmaker, league_id: str) -> set[str]:
    """Return the set of `player_id`s currently on the watchlist of `league_id`.

    One query (primary key lookup) – used by every page that renders a star, so the
    caller can mark watched players in O(1) per row."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Watchlist.player_id).where(Watchlist.league_id == league_id)
            )
        ).all()
    return {row[0] for row in rows}


class WatchlistError(Exception):
    """Base class for watchlist service errors."""


class PlayerNotFound(WatchlistError):
    def __init__(self, player_id: str) -> None:
        super().__init__(f"Spieler {player_id} ist nicht im Kader bekannt")
        self.player_id = player_id


class WatchlistEntryMissing(WatchlistError):
    def __init__(self, league_id: str, player_id: str) -> None:
        super().__init__(f"Spieler {player_id} steht nicht auf der Merkliste")
        self.league_id = league_id
        self.player_id = player_id


@dataclass(frozen=True)
class WatchlistItem:
    """Row from the watchlist plus a snapshot of the player's current data."""

    entry: Watchlist
    player_name: str
    market_value: int
    status: int

    @property
    def availability(self) -> Availability:
        """Coarse availability for the row (used by the badge colour and label)."""
        from app.kickbase.models import availability

        return availability(self.status)

    @property
    def status_label(self) -> str:
        """Short German label (matches the badges on the player page)."""
        from app.kickbase.models import status_label

        return status_label(self.status)


async def add(
    sessions: async_sessionmaker,
    league_id: str,
    player_id: str,
    now: datetime,
) -> Watchlist:
    """Add `player_id` to the watchlist of `league_id`. Idempotent: re-adding an existing
    entry returns the same row without touching `added_at` or `price_below`. Raises
    `PlayerNotFound` if the player is not known yet (defends against UI typos)."""
    async with sessions.begin() as session:
        player = await session.get(Player, player_id)
        if player is None:
            raise PlayerNotFound(player_id)
        existing = await session.get(Watchlist, (league_id, player_id))
        if existing is not None:
            return existing
        entry = Watchlist(league_id=league_id, player_id=player_id, added_at=now)
        session.add(entry)
        return entry


async def remove(sessions: async_sessionmaker, league_id: str, player_id: str) -> bool:
    """Remove `player_id` from the watchlist of `league_id`. Returns `True` if a row was
    deleted, `False` if it wasn't there."""
    async with sessions.begin() as session:
        entry = await session.get(Watchlist, (league_id, player_id))
        if entry is None:
            return False
        await session.delete(entry)
    return True


async def list_entries(sessions: async_sessionmaker, league_id: str) -> list[WatchlistItem]:
    """Return all watchlist entries of `league_id` with the player's current name, market
    value and status. Ordered by `added_at` (oldest first)."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Watchlist, Player.name, Player.market_value, Player.status)
                .join(Player, Player.id == Watchlist.player_id)
                .where(Watchlist.league_id == league_id)
                .order_by(Watchlist.added_at)
            )
        ).all()
    return [
        WatchlistItem(
            entry=entry,
            player_name=name,
            market_value=value,
            status=status_int,
        )
        for entry, name, value, status_int in rows
    ]


async def set_threshold(
    sessions: async_sessionmaker,
    league_id: str,
    player_id: str,
    price_below: int | None,
) -> Watchlist:
    """Set or clear the `price_below` alarm for `player_id`. `None` removes the threshold.
    Raises `WatchlistEntryMissing` if the player is not on the watchlist."""
    async with sessions.begin() as session:
        entry = await session.get(Watchlist, (league_id, player_id))
        if entry is None:
            raise WatchlistEntryMissing(league_id, player_id)
        entry.price_below = price_below
    return entry
