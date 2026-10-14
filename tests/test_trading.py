from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from app.db.models import Base, MarketValue
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.calendar import market_day
from app.kickbase.league import ResolvedLeague
from app.kickbase.models import Position
from app.services.holding import holding_signal
from app.services.momentum import Momentum, daily_change, load_momentum
from app.services.players import StartProbability
from app.services.recommendations import Candidate, Recommendation, load_basis
from app.services.scoring import Score, TradingConfig
from app.services.trading import trade_opportunity, trading_view, update_count
from tests.fakes import (
    NOW,
    TEAM_LEAGUE_ID,
    FakeOpenLigaDb,
    add_recommendation_endpoints,
    fake_with_team_data,
    fixture,
    make_client,
    make_oldb_client,
)


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'trading.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


def signal(delta, *, start=15, hours=72):
    return holding_signal(
        current=5_000_000,
        change_24h=delta,
        start=StartProbability(start, ("nicht in der Prognose-Elf",)),
        deadline=NOW + timedelta(hours=hours) if hours is not None else None,
        now=NOW,
        config=TradingConfig(),
    )


def test_rising_nonstarter_is_held_during_the_week_not_sold_for_points():
    result = signal(100_000)
    assert not result.sell and result.trading
    assert result.verdict == "Trading halten"
    assert "vor der Deadline" in result.reasons[0]


def test_near_deadline_points_take_priority_over_a_rising_nonstarter():
    assert signal(100_000, hours=23).sell
    assert not signal(100_000, hours=25).sell


def test_falling_current_trend_is_a_sell_signal_even_with_a_good_start():
    assert signal(-100_000, start=85).sell
    assert not signal(0, start=85).sell


def test_missing_daily_data_is_unknown_not_a_weekly_sell_signal():
    result = signal(None, start=85)
    assert not result.sell and "unbekannt" in result.verdict
    assert signal(100_000, hours=None).sell  # cannot rely on an unknown exit deadline


async def test_daily_momentum_requires_the_exact_previous_market_day(sessions):
    day = market_day(NOW)
    async with sessions.begin() as session:
        session.add_all(
            [
                MarketValue(player_id="daily", day=day - timedelta(days=1), value=1_000_000),
                MarketValue(player_id="gap", day=day - timedelta(days=2), value=800_000),
                MarketValue(player_id="daily", day=day - timedelta(days=3), value=900_000),
            ]
        )
    data = await load_momentum(sessions, {"daily": 1_100_000, "gap": 1_000_000}, day)
    assert data["daily"].change_24h == 100_000
    assert data["daily"].percent_24h == pytest.approx(10)
    assert data["daily"].change_3d == 200_000
    assert data["gap"].change_24h is None


def test_missing_api_field_is_not_treated_as_a_flat_day():
    from app.kickbase.models import SquadPlayer

    raw = fixture("league_squad")["it"][0]
    raw.pop("tfhmvt", None)
    assert daily_change(SquadPlayer.model_validate(raw)) is None
    raw["tfhmvt"] = 0
    assert daily_change(SquadPlayer.model_validate(raw)) == 0


def candidate(**kwargs):
    rec = Recommendation(
        player=Candidate("trade", "Trade", "1", None, Position.FORWARD, 0, None),
        score=Score(5, (), False),
        market_value=1_000_000,
        price=1_100_000,
        seller="Kickbase",
        seller_is_kickbase=True,
        expires_at=NOW + timedelta(hours=1),
        momentum=Momentum(100_000, 10),
    )
    return replace(rec, **kwargs)


def opportunity(rec, **kwargs):
    params = dict(
        available=5_000_000,
        next_update=NOW + timedelta(hours=8),
        sell_by=NOW + timedelta(days=3),
        now=NOW,
        min_gain=20_000,
    )
    return trade_opportunity(rec, **(params | kwargs))


def test_trade_scenario_deducts_the_purchase_premium():
    result = opportunity(candidate())
    assert result.updates == 3
    assert result.break_even_updates == 1
    assert result.scenario_profit == 200_000
    assert opportunity(candidate(price=1_500_000)) is None


@pytest.mark.parametrize(
    "rec",
    [
        candidate(seller="Manager N"),
        candidate(price=0),
        candidate(momentum=Momentum(None, None)),
        candidate(expires_at=NOW + timedelta(days=5)),
        candidate(momentum=Momentum(-100_000, -10)),
    ],
)
def test_unproven_or_unprofitable_trades_are_not_recommended(rec):
    assert opportunity(rec) is None


def test_own_bid_is_not_deducted_twice_for_a_trade():
    assert opportunity(candidate(my_offer=1_100_000), available=0) is not None
    assert opportunity(candidate(), available=0) is None


def test_unknown_update_or_no_time_left_has_no_scenario():
    assert opportunity(candidate(), next_update=None) is None
    assert opportunity(candidate(), sell_by=NOW + timedelta(minutes=30)) is None
    assert update_count(NOW + timedelta(hours=1), NOW, NOW + timedelta(hours=1)) == 0
    assert opportunity(candidate(), next_update=NOW - timedelta(hours=1)) is None
    assert update_count(NOW, NOW, NOW + timedelta(days=2)) == 1


async def test_during_market_value_switch_daily_snapshot_delta_is_unknown(sessions):
    from app.services.recommendations import buy_view
    from tests.fakes import FakeClock

    now = NOW.replace(hour=20, minute=5)
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    listing = fixture("league_market")["it"][0]
    async with sessions.begin() as session:
        session.add(
            MarketValue(player_id=listing["i"], day=market_day(now) - timedelta(days=1), value=1)
        )
    league = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")
    async with (
        make_client(fake, clock=FakeClock(now)) as client,
        make_oldb_client(FakeOpenLigaDb()) as oldb,
    ):
        basis = await load_basis(client, oldb, sessions, league, now)
        data = await buy_view(client, sessions, league, basis, now=now)
    assert data.scored
    assert all(r.momentum.change_24h is None for r in data.scored)
    assert any("Marktwert-Update läuft" in note for note in basis.notes)


async def test_funding_plan_counts_open_bids_and_never_future_gains(sessions):
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    budget = fixture("league_budget")
    budget["b"], budget["pbaa"] = 2_000_000, -3_000_000
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    league = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        basis = await load_basis(client, oldb, sessions, league, NOW)
        data = await trading_view(client, sessions, league, basis, now=NOW)
    assert data.deficit == 3_000_000
    assert data.funding and data.funding.covers
    assert data.funding.total >= data.deficit
    assert data.funding.surplus == data.funding.total - 3_000_000
    assert not data.opportunities  # no free budget; hypothetical rises cannot finance orders
    assert len({p.id for p in (*data.protected, *data.holdings)}) == len(
        fixture("league_squad")["it"]
    )


def test_trading_page_is_available_and_has_a_real_deadline_plan(logged_in_client):
    response = logged_in_client.get("/empfehlungen/trading")
    assert response.status_code == 200
    assert "Plan bis zur Deadline" in response.text
    assert "Trading-Positionen" in response.text
    assert "Nach allen Geboten" in response.text
    assert "keine Prognose" in response.text or "keine belegte Trading-Chance" in response.text


def test_manager_named_kickbase_is_not_a_timed_trade():
    assert opportunity(candidate(seller="Kickbase", seller_is_kickbase=False)) is None
