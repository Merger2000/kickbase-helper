import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import Base, LeagueTransfer, MarketValue
from app.db.session import make_engine, make_sessionmaker
from app.kickbase import models as m
from app.kickbase.calendar import market_day
from app.kickbase.league import ResolvedLeague
from app.services.recommendations import (
    BuyFilters,
    BuyView,
    Candidate,
    Recommendation,
    select_buys,
    squad_gaps,
)
from app.services.scoring import BidConfig, Profile, Score, Weights, load_scoring_config
from app.services.transfers import (
    PAGE_SIZE,
    BidModel,
    collect_transfers,
    fill_market_values,
    load_bid_model,
    quantile,
)
from tests.fakes import TEAM_LEAGUE_ID, FakeKickbase, fixture, make_client

FEED = fixture("league_activities_transfers")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LEAGUE = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")
PATH = f"/v4/leagues/{TEAM_LEAGUE_ID}/activitiesFeed"
ME = "Manager 1"  # the anonymised owner in the fixtures


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'transfers.db'}"
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


def paged(pages: list[dict]):
    """Answer the feed by its start parameter: page n = pages[n], beyond that an empty page."""

    def respond(request):
        index = int(request.url.params["start"]) // PAGE_SIZE
        return pages[index] if index < len(pages) else {"af": []}

    return respond


async def count(sessions, *where) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(LeagueTransfer).where(*where))


async def test_collect_stores_transfers_without_buyer_names(sessions):
    fake = FakeKickbase()
    fake.on("GET", PATH, paged([FEED]))
    async with make_client(fake) as client:
        result = await collect_transfers(client, sessions, LEAGUE, ME, NOW, 60)
    assert (result.new, result.pages) == (50, 2)  # a full page → one more, which is empty
    assert await count(sessions) == 50
    assert await count(sessions, LeagueTransfer.kind == 1) == 26
    mine = sum(1 for a in FEED["af"] if a["data"]["t"] == 1 and a["data"].get("byr") == ME)
    assert await count(sessions, LeagueTransfer.by_me) == mine
    own_sales = sum(1 for a in FEED["af"] if a["data"].get("slr") == ME)
    assert await count(sessions, LeagueTransfer.sold_by_me.is_(True)) == own_sales
    assert await count(sessions, LeagueTransfer.sold_by_me.is_(False)) == 24 - own_sales
    # No column holds a manager name.
    columns = {c.name for c in LeagueTransfer.__table__.columns}
    assert not columns & {"buyer", "byr", "buyer_name"}

    # Second run: the first page is known already → one request, nothing new.
    async with make_client(fake) as client:
        again = await collect_transfers(client, sessions, LEAGUE, ME, NOW, 60)
    assert (again.new, again.pages) == (0, 1)


async def test_collect_stops_at_the_period(sessions):
    fake = FakeKickbase()
    fake.on("GET", PATH, paged([FEED, FEED]))
    newest = max(datetime.fromisoformat(a["dt"]) for a in FEED["af"])
    async with make_client(fake) as client:
        result = await collect_transfers(
            client, sessions, LEAGUE, ME, newest + timedelta(days=2), 3
        )
    # Only transfers of the last 3 days; the old ones end the paging.
    assert result.pages == 1
    assert result.new == sum(
        1 for a in FEED["af"] if datetime.fromisoformat(a["dt"]) >= newest - timedelta(days=1)
    )


async def test_market_values_of_the_purchase_day(sessions):
    first = FEED["af"][0]
    at = datetime.fromisoformat(first["dt"])
    player = first["data"]["pi"]
    async with sessions.begin() as session:
        session.add(
            LeagueTransfer(
                activity_id="1",
                league_id=TEAM_LEAGUE_ID,
                at=at,
                player_id=player,
                player_name="X",
                team_id=None,
                kind=1,
                price=1,
                by_me=False,
            )
        )
        # Only a value two days before the market day.
        session.add(MarketValue(player_id=player, day=market_day(at) - timedelta(days=2), value=7))
    # The purchase day itself may still be synced: wait instead of using an older value.
    assert await fill_market_values(sessions, TEAM_LEAGUE_ID) == 0
    async with sessions.begin() as session:  # a later day is synced: the day won't come any more
        session.add(MarketValue(player_id="other", day=market_day(at) + timedelta(days=1), value=1))
    assert await fill_market_values(sessions, TEAM_LEAGUE_ID) == 1
    async with sessions() as session:
        assert (await session.get(LeagueTransfer, "1")).market_value == 7
    assert await fill_market_values(sessions, TEAM_LEAGUE_ID) == 0


async def test_exact_day_is_used_as_soon_as_it_is_synced(sessions):
    first = FEED["af"][0]
    at = datetime.fromisoformat(first["dt"])
    player = first["data"]["pi"]
    async with sessions.begin() as session:
        session.add(
            LeagueTransfer(
                activity_id="1",
                league_id=TEAM_LEAGUE_ID,
                at=at,
                player_id=player,
                player_name="X",
                team_id=None,
                kind=1,
                price=1,
                by_me=False,
            )
        )
        session.add(MarketValue(player_id=player, day=market_day(at) - timedelta(days=1), value=6))
        session.add(MarketValue(player_id=player, day=market_day(at), value=9))
    assert await fill_market_values(sessions, TEAM_LEAGUE_ID) == 1
    async with sessions() as session:
        assert (await session.get(LeagueTransfer, "1")).market_value == 9


# ---------------------------------------------------------------------------- bid model

CONFIG = BidConfig(
    quantile=0.7, period_days=60, price_bounds=[1_000_000, 5_000_000], min_comparable=3
)


def test_quantile_interpolates():
    assert quantile([0.0, 0.1, 0.2, 0.3], 0.5) == pytest.approx(0.15)
    assert quantile([0.05], 0.9) == 0.05
    with pytest.raises(ValueError):
        quantile([], 0.5)


def test_advise_uses_the_price_class_and_rounds_up():
    model = BidModel(CONFIG, ((), (0.0, 0.05, 0.1, 0.2), ()))
    advice = model.advise(2_000_000, 2_000_000)
    # 70 % quantile of [0, 0.05, 0.1, 0.2] = 0.1 + 0.1 · 0.1 = 0.11
    assert advice.premium == pytest.approx(0.11)
    assert advice.amount == 2_220_000
    assert (advice.comparable, advice.same_class, advice.quantile_percent) == (4, True, 70)
    assert advice.median_premium == pytest.approx(0.075)


def test_advise_falls_back_to_all_classes_and_respects_the_price():
    model = BidModel(CONFIG, ((0.0,), (0.1,), (0.3, 0.4)))
    advice = model.advise(10_000_000, 15_000_000)  # class 2 has only 2 < 3
    assert not advice.same_class and advice.comparable == 4
    assert advice.amount == 15_000_000  # never below the asking price
    # The shown premium belongs to the shown amount; the model's own one is kept apart.
    assert advice.at_asking_price and advice.premium == pytest.approx(0.5)
    assert advice.model_premium == pytest.approx(0.31)  # 70 % of [0, 0.1, 0.3, 0.4]
    normal = model.advise(10_000_000, 10_000_000)
    assert not normal.at_asking_price
    assert normal.premium == pytest.approx(normal.model_premium, abs=1e-3)
    assert BidModel(CONFIG, ((0.0,), (), ())).advise(500_000, 500_000) is None
    assert model.advise(0, 0) is None


async def test_load_bid_model_uses_purchases_in_the_period(sessions):
    def transfer(i, *, kind=1, days_ago=1, price=1_100_000, value=1_000_000):
        return LeagueTransfer(
            activity_id=str(i),
            league_id=TEAM_LEAGUE_ID,
            at=NOW - timedelta(days=days_ago),
            player_id=str(i),
            player_name="X",
            team_id=None,
            kind=kind,
            price=price,
            by_me=False,
            market_value=value,
        )

    async with sessions.begin() as session:
        session.add_all(
            [
                transfer(1),  # +10 % in class 1
                transfer(2, price=6_000_000, value=6_000_000),  # 0 % in class 2
                transfer(3, kind=2),  # sold to Kickbase: not a bid
                transfer(4, days_ago=90),  # too old
                transfer(5, value=None),  # no market value yet
            ]
        )
    model = await load_bid_model(sessions, TEAM_LEAGUE_ID, NOW, CONFIG)
    assert model.by_class == ((), (pytest.approx(0.1),), (0.0,))


def test_scoring_config_has_bid_defaults():
    bids = load_scoring_config().bids
    assert bids.quantile == 0.7
    with pytest.raises(ValueError):
        BidConfig(quantile=0.7, period_days=60, price_bounds=[5, 1], min_comparable=3)


# ---------------------------------------------------------------------------- buy view


def player(status=0, position=4):
    return type("P", (), {"status": status, "position": position})()


def test_squad_gaps_ignore_injured_players():
    squad = [player(position=1)] + [player(position=2)] * 4 + [player(position=3)] * 4
    squad += [player(position=4), player(status=1, position=4)]  # one forward is out
    assert squad_gaps(squad) == {m.Position.FORWARD: 1}  # 4-4-2 needs one more
    full = [player(position=1)] + [player(position=2)] * 4 + [player(position=3)] * 4
    assert squad_gaps(full + [player(position=4)] * 2) == {}


def rec(name, position, price, score, *, before=True, per_million=None, expires=None):
    profile = Profile(weights=Weights(form=0.4, trend=0.35, programm=0.25, einsatz=0.0, status=1.0))
    return Recommendation(
        player=Candidate(name, name, "1", "Club", m.Position(position), 0, None),
        score=Score(total=score, components=(), clamped=False),
        market_value=price,
        price=price,
        per_million=per_million if per_million is not None else score / (price / 1e6),
        expires_at=expires,
        before_deadline=before,
    ), profile


def view(*recs, gaps=None) -> BuyView:
    return BuyView(
        budget=50_000_000,
        recommendations=tuple(r for r, _ in recs),
        over_budget=0,
        below_min_score=0,
        without_appearances=0,
        min_score=40,
        profile=recs[0][1] if recs else None,
        gaps=gaps or {},
    )


def test_select_buys_filters_and_sorts():
    data = view(
        rec("A", 4, 5_000_000, 80),
        rec("B", 3, 1_000_000, 50),
        rec("C", 4, 2_000_000, 60, before=False),
        rec("D", 2, 9_000_000, 90, before=None),  # deadline unknown: kept
        gaps={m.Position.FORWARD: 1},
    )
    names = lambda shown: [r.player.name for r in shown[0]]  # noqa: E731
    assert names(select_buys(data, BuyFilters())) == ["B", "A", "D"]  # per million, C ends late
    assert names(select_buys(data, BuyFilters(before_deadline=False))) == ["B", "C", "A", "D"]
    assert names(select_buys(data, BuyFilters(sort="score"))) == ["D", "A", "B"]
    assert names(select_buys(data, BuyFilters(sort="preis"))) == ["B", "A", "D"]
    assert names(select_buys(data, BuyFilters(position=4))) == ["A"]
    assert names(select_buys(data, BuyFilters(gaps_only=True, before_deadline=False))) == ["C", "A"]
    assert names(select_buys(data, BuyFilters(max_price=2_000_000))) == ["B"]
    assert select_buys(data, BuyFilters())[1] == 3


def test_select_buys_sort_by_end():
    soon = datetime(2026, 10, 7, 18, tzinfo=UTC)
    later = soon + timedelta(hours=5)
    data = view(
        rec("late", 4, 1_000_000, 50, expires=later),
        rec("none", 4, 1_000_000, 50, expires=None),
        rec("soon", 4, 1_000_000, 50, expires=soon),
    )
    assert [r.player.name for r in select_buys(data, BuyFilters(sort="ende"))[0]] == [
        "soon",
        "late",
        "none",
    ]


def test_collect_job_summary_uses_my_name():
    # The owner in user_me.json is the same anonymised manager as in the transfer feed.
    assert fixture("user_me")["u"]["name"] == ME
    assert any(a["data"].get("byr") == ME for a in FEED["af"])


async def test_collect_job(sessions):
    from zoneinfo import ZoneInfo

    from app.kickbase.league import LeagueContext
    from app.worker.jobs import JobContext, collect_league_transfers
    from tests.fakes import FakeClock, MemoryKV

    fake = FakeKickbase()
    fake.on("GET", "/v4/user/me", fixture("user_me"))
    fake.on("GET", "/v4/leagues/selection", fixture("leagues_selection"))
    fake.on("GET", PATH, paged([FEED]))
    async with make_client(fake) as client:
        kv = MemoryKV()
        ctx = JobContext(
            client=client,
            sessions=sessions,
            league=LeagueContext(client, kv, "Testliga"),
            kv=kv,
            tz=ZoneInfo("Europe/Berlin"),
            clock=FakeClock(NOW),
        )
        assert (
            await collect_league_transfers(ctx) == "50 neue Transfers (2 Seiten), 0 mit Marktwert"
        )


async def test_unreadable_transfer_is_skipped_without_leaking_names(sessions, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    page = {"af": [dict(a) for a in FEED["af"]]}
    broken = dict(page["af"][0])
    broken["data"] = {"byr": "Geheimer Manager", "pi": "1", "t": 1}  # no price
    page["af"][0] = broken
    fake = FakeKickbase()
    fake.on("GET", PATH, paged([page]))
    async with make_client(fake) as client:
        result = await collect_transfers(client, sessions, LEAGUE, ME, NOW, 60)
    assert (result.new, result.unreadable) == (49, 1)
    assert "Geheimer Manager" not in caplog.text
    assert f"Transfer {broken['i']} nicht lesbar" in caplog.text


async def test_bid_status_hints(sessions):
    from app.db.models import JobRun
    from app.services.transfers import load_bid_status

    status = await load_bid_status(sessions, TEAM_LEAGUE_ID, NOW, CONFIG)
    assert status.hint == (
        "Die Liga-Transfers wurden noch nicht gesammelt. "
        "Gebotsempfehlung ab 3 Käufen mit Marktwert; bisher 0."
    )
    async with sessions.begin() as session:
        session.add(
            JobRun(job="collect_league_transfers", started_at=NOW, finished_at=NOW, status="ok")
        )
        for i in range(2):
            session.add(
                LeagueTransfer(
                    activity_id=f"w{i}",
                    league_id=TEAM_LEAGUE_ID,
                    at=NOW - timedelta(hours=1),
                    player_id="1",
                    player_name="X",
                    team_id=None,
                    kind=1,
                    price=1,
                    by_me=False,
                )
            )
    status = await load_bid_status(sessions, TEAM_LEAGUE_ID, NOW, CONFIG)
    assert status.hint == (
        "Gebotsempfehlung ab 3 Käufen mit Marktwert; bisher 0, 2 warten noch auf ihren Marktwert."
    )
