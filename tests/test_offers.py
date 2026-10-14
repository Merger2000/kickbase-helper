import copy
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Base, LeagueTransfer, MyOffer
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.league import ResolvedLeague
from app.services.offers import NO_SALE_AFTER, offer_stats, track_offers
from tests.fakes import TEAM_LEAGUE_ID, FakeClock, FakeKickbase, fixture, make_client

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
LEAGUE = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")
MARKET = f"/v4/leagues/{TEAM_LEAGUE_ID}/market"
SQUAD = f"/v4/leagues/{TEAM_LEAGUE_ID}/squad"


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'offers.db'}"
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


def market_with_offers() -> dict:
    """The market fixture with exactly three own bids, all on Kickbase listings."""
    data = copy.deepcopy(fixture("league_market"))
    listings = [p for p in data["it"] if not p.get("u")][:3]
    for p in data["it"]:
        p.pop("uop", None)
    for p, amount in zip(listings, (2_000_000, 3_000_000, 4_000_000), strict=True):
        p["uop"] = amount
        p["exs"] = 3600
    return data


def squad_with(*player_ids: str) -> dict:
    data = copy.deepcopy(fixture("league_squad"))
    template = data["it"][0]
    for player_id in player_ids:
        data["it"].append(dict(template, i=player_id))
    return data


async def run(sessions, fake, clock):
    async with make_client(fake, clock=clock) as client:
        return await track_offers(client, sessions, LEAGUE, clock())


async def offers(sessions) -> dict[str, MyOffer]:
    async with sessions() as session:
        return {o.player_id: o for o in (await session.execute(select(MyOffer))).scalars()}


async def test_offers_are_tracked_and_evaluated(sessions):
    market = market_with_offers()
    won, lost, unsold = (p["i"] for p in market["it"] if p.get("uop"))
    fake = FakeKickbase()
    fake.on("GET", MARKET, market)
    clock = FakeClock(NOW)
    result = await run(sessions, fake, clock)
    assert (result.new, result.evaluated) == (3, 0)
    assert not fake.gets(SQUAD)  # nothing ended: no squad request

    # Two hours later all three offers have ended.
    clock.advance(hours=2)
    fake.on("GET", MARKET, {"it": [], "dt": market["dt"]})
    fake.on("GET", SQUAD, squad_with(won))
    async with sessions.begin() as session:
        session.add(
            LeagueTransfer(
                activity_id="a1",
                league_id=TEAM_LEAGUE_ID,
                at=NOW + timedelta(minutes=50),
                player_id=lost,
                player_name="X",
                team_id=None,
                kind=1,
                price=3_600_000,
                by_me=False,
            )
        )
    result = await run(sessions, fake, clock)
    assert result.evaluated == 2  # the unsold one waits for late transfers
    stored = await offers(sessions)
    assert (stored[won].outcome, stored[won].final_price) == ("won", 2_000_000)
    assert (stored[lost].outcome, stored[lost].final_price) == ("lost", 3_600_000)
    assert stored[unsold].outcome == "open"

    clock.advance(seconds=NO_SALE_AFTER.total_seconds())
    result = await run(sessions, fake, clock)
    assert result.evaluated == 1
    assert (await offers(sessions))[unsold].outcome == "none"

    stats = await offer_stats(sessions, TEAM_LEAGUE_ID, clock())
    assert (stats.won, stats.lost, stats.none, stats.open) == (1, 1, 1, 0)
    assert stats.lost_gap == pytest.approx(0.2)  # 3.6 M against 3.0 M
    assert stats.decided == 2
    assert len(stats.recent) == 3


async def test_raised_bid_keeps_the_last_amount(sessions):
    market = market_with_offers()
    fake = FakeKickbase()
    fake.on("GET", MARKET, market)
    clock = FakeClock(NOW)
    await run(sessions, fake, clock)
    raised = copy.deepcopy(market)
    first = next(p for p in raised["it"] if p.get("uop"))
    first["uop"] = 2_500_000
    fake.on("GET", MARKET, raised)
    await run(sessions, fake, clock)
    assert (await offers(sessions))[first["i"]].amount == 2_500_000


async def test_withdrawn_bid_is_evaluated(sessions):
    market = market_with_offers()
    fake = FakeKickbase()
    fake.on("GET", MARKET, market)
    clock = FakeClock(NOW)
    await run(sessions, fake, clock)
    withdrawn = copy.deepcopy(market)
    for p in withdrawn["it"]:
        p.pop("uop", None)
    fake.on("GET", MARKET, withdrawn)
    fake.on("GET", SQUAD, fixture("league_squad"))
    clock.advance(seconds=NO_SALE_AFTER.total_seconds() + 3600)
    result = await run(sessions, fake, clock)
    assert result.evaluated == 3
    assert {o.outcome for o in (await offers(sessions)).values()} == {"none"}


def manager_market_with_offer() -> tuple[dict, str]:
    """One own bid on an endless manager listing (`exs` is None)."""
    data = copy.deepcopy(fixture("league_market"))
    for p in data["it"]:
        p.pop("uop", None)
    listing = next(p for p in data["it"] if p.get("u"))
    listing["uop"] = 5_000_000
    return data, listing["i"]


async def test_manager_listing_vanished_becomes_none(sessions):
    market, pid = manager_market_with_offer()
    fake = FakeKickbase()
    fake.on("GET", MARKET, market)
    clock = FakeClock(NOW)
    result = await run(sessions, fake, clock)
    assert (result.new, result.evaluated) == (1, 0)
    assert (await offers(sessions))[pid].expires_at is None  # endless manager listing

    # The listing vanishes from the market without a sale.
    gone = copy.deepcopy(market)
    gone["it"] = [p for p in gone["it"] if p["i"] != pid]
    fake.on("GET", MARKET, gone)
    fake.on("GET", SQUAD, fixture("league_squad"))
    clock.advance(hours=2)
    result = await run(sessions, fake, clock)
    assert result.evaluated == 0  # still within the grace period for late transfers
    assert (await offers(sessions))[pid].outcome == "open"

    clock.advance(seconds=NO_SALE_AFTER.total_seconds())
    result = await run(sessions, fake, clock)
    assert result.evaluated == 1
    assert (await offers(sessions))[pid].outcome == "none"


async def test_won_via_own_transfer_uses_transfer_price(sessions):
    market = market_with_offers()
    pid = next(p["i"] for p in market["it"] if p.get("uop"))
    fake = FakeKickbase()
    fake.on("GET", MARKET, market)
    clock = FakeClock(NOW)
    await run(sessions, fake, clock)

    gone = copy.deepcopy(market)
    gone["it"] = [p for p in gone["it"] if p["i"] != pid]
    fake.on("GET", MARKET, gone)
    fake.on("GET", SQUAD, squad_with(pid))
    async with sessions.begin() as session:
        session.add(
            LeagueTransfer(
                activity_id="a2",
                league_id=TEAM_LEAGUE_ID,
                at=NOW + timedelta(minutes=30),
                player_id=pid,
                player_name="X",
                team_id=None,
                kind=1,
                price=4_400_000,
                by_me=True,
            )
        )
    clock.advance(hours=2)
    result = await run(sessions, fake, clock)
    stored = await offers(sessions)
    assert (stored[pid].outcome, stored[pid].final_price) == ("won", 4_400_000)
    assert result.evaluated == 1


def test_listing_key_ignores_the_bidder_id():
    from app.kickbase import models as m

    market = m.Market.model_validate(fixture("league_market"))
    keys = [p.listing_key for p in market.players]
    assert len(keys) == len(set(keys))
