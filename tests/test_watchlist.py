"""Tests for the watchlist service (Block C, C1.1)."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Base, Player, Watchlist
from app.db.session import make_engine, make_sessionmaker
from app.services.watchlist import (
    PlayerNotFound,
    WatchlistEntryMissing,
    add,
    list_entries,
    remove,
    set_threshold,
)

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
LEAGUE = "league-1"
MUSTER = "1"
HANSEN = "2"
GHOST = "999"


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'watchlist.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_players(sessions) -> None:
    async with sessions.begin() as session:
        session.add_all(
            [
                Player(
                    id=MUSTER,
                    name="Muster",
                    team_id="t1",
                    position=1,
                    status=1,
                    market_value=2_000_000,
                    updated_at=NOW,
                ),
                Player(
                    id=HANSEN,
                    name="Hansen",
                    team_id="t1",
                    position=1,
                    status=1,
                    market_value=5_000_000,
                    updated_at=NOW,
                ),
            ]
        )


async def test_add_is_idempotent(sessions):
    await _seed_players(sessions)
    first = await add(sessions, LEAGUE, MUSTER, NOW)
    second = await add(sessions, LEAGUE, MUSTER, NOW + timedelta(hours=1))
    assert first.added_at == second.added_at
    rows = await list_entries(sessions, LEAGUE)
    assert len(rows) == 1
    assert rows[0].player_name == "Muster"
    assert rows[0].market_value == 2_000_000


async def test_remove_returns_false_when_missing(sessions):
    await _seed_players(sessions)
    assert await remove(sessions, LEAGUE, MUSTER) is False
    await add(sessions, LEAGUE, MUSTER, NOW)
    assert await remove(sessions, LEAGUE, MUSTER) is True
    assert await remove(sessions, LEAGUE, MUSTER) is False
    assert await list_entries(sessions, LEAGUE) == []


async def test_add_unknown_player_raises(sessions):
    with pytest.raises(PlayerNotFound):
        await add(sessions, LEAGUE, GHOST, NOW)
    assert await list_entries(sessions, LEAGUE) == []


async def test_set_threshold_set_and_clear(sessions):
    await _seed_players(sessions)
    await add(sessions, LEAGUE, MUSTER, NOW)
    updated = await set_threshold(sessions, LEAGUE, MUSTER, 1_500_000)
    assert updated.price_below == 1_500_000
    cleared = await set_threshold(sessions, LEAGUE, MUSTER, None)
    assert cleared.price_below is None
    with pytest.raises(WatchlistEntryMissing):
        await set_threshold(sessions, LEAGUE, HANSEN, 1_000_000)
    async with sessions() as session:
        row = await session.get(Watchlist, (LEAGUE, MUSTER))
        assert row is not None and row.price_below is None
