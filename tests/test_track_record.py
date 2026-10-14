from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from app.db.models import Base, MarketValue, MatchdayPoints, RecommendationLog
from app.db.session import make_engine, make_sessionmaker
from app.kickbase import models as m
from app.services.recommendations import BuyView, Candidate, Recommendation, SellView
from app.services.scoring import Component, Profile, Score, Weights
from app.services.track_record import log_recommendations, track_record

LEAGUE = "1000002"
# 08:15 Berlin on 1.10.: the current market values are those published on the evening of
# 30.9., so the snapshot belongs to market day 30.9.
SNAPSHOT = datetime(2026, 10, 1, 6, 15, tzinfo=UTC)
DAY = date(2026, 9, 30)


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'record.db'}"
    sync = create_engine(url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


PROFILE = Profile(
    weights=Weights(form=0.4, trend=0.35, programm=0.25, einsatz=0.0, status=1.0), min_score=40
)


def rec(player_id: str, score: float, value: int, price: int | None = None) -> Recommendation:
    components = (Component("form", "Form", 0.5, 0.4, score, "x"),)
    return Recommendation(
        player=Candidate(player_id, player_id, "1", "Club", m.Position.FORWARD, 0, None),
        score=Score(total=score, components=components, clamped=False),
        market_value=value,
        price=price,
        per_million=score / (price / 1e6) if price else None,
        before_deadline=True,
    )


def buy_view(*recs) -> BuyView:
    return BuyView(
        budget=50_000_000,
        recommendations=tuple(r for r in recs if r.score.total >= 40),
        over_budget=0,
        below_min_score=0,
        without_appearances=0,
        min_score=40,
        profile=PROFILE,
        scored=tuple(recs),
    )


def sell_view(flagged, others) -> SellView:
    return SellView(
        recommendations=tuple(flagged),
        unflagged=len(others),
        threshold=-3,
        profile=PROFILE,
        scored=tuple(flagged) + tuple(others),
    )


async def test_snapshot_logs_everything_once_per_day(sessions):
    buys = buy_view(rec("b1", 70, 1_000_000, 1_000_000), rec("b2", 20, 2_000_000, 2_000_000))
    sells = sell_view([rec("s1", 10, 5_000_000)], [rec("s2", 60, 6_000_000)])
    assert await log_recommendations(sessions, LEAGUE, SNAPSHOT, buys, sells) == 4
    assert await log_recommendations(sessions, LEAGUE, SNAPSHOT, buys, sells) == 4  # replaced
    async with sessions() as session:
        rows = list((await session.execute(select(RecommendationLog))).scalars())
        assert len(rows) == 4
    flags = {(r.view, r.player_id): r.recommended for r in rows}
    assert flags == {
        ("kaufen", "b1"): True,
        ("kaufen", "b2"): False,  # below the minimum score: not recommended
        ("verkaufen", "s1"): True,
        ("verkaufen", "s2"): False,
    }
    assert {r.day for r in rows} == {DAY}
    assert next(r for r in rows if r.player_id == "b1").components == {"form": 70}


async def seed_values(sessions, values: dict[str, dict[int, int]]):
    async with sessions.begin() as session:
        for player, by_offset in values.items():
            for offset, value in by_offset.items():
                session.add(
                    MarketValue(player_id=player, day=DAY + timedelta(days=offset), value=value)
                )


async def test_track_record_compares_recommended_with_the_rest(sessions):
    buys = buy_view(rec("b1", 70, 1_000_000, 1_000_000), rec("b2", 20, 2_000_000, 2_000_000))
    sells = sell_view([rec("s1", 10, 5_000_000)], [rec("s2", 60, 6_000_000)])
    await log_recommendations(sessions, LEAGUE, SNAPSHOT, buys, sells)
    await seed_values(
        sessions,
        {
            "b1": {1: 1_100_000, 3: 1_200_000},  # no value on day 7 → not counted there
            "b2": {1: 1_900_000, 3: 2_000_000, 7: 2_200_000},
            "s1": {1: 4_500_000},
            "s2": {1: 6_000_000},
        },
    )
    async with sessions.begin() as session:
        for player, points, days in (("b1", 80, 2), ("b1", 40, 9), ("b2", 10, 3)):
            session.add(
                MatchdayPoints(
                    season=2026,
                    matchday=days,
                    player_id=player,
                    points=points,
                    team_id="1",
                    kickoff=SNAPSHOT + timedelta(days=days),
                )
            )
    record = await track_record(sessions, LEAGUE, SNAPSHOT + timedelta(days=8))
    assert record.first_day == DAY and not record.empty
    buy = record.views["kaufen"]
    one, three, seven = buy.horizons
    assert one.recommended.mean_change == pytest.approx(0.10)
    assert one.others.mean_change == pytest.approx(-0.05)
    assert one.difference == pytest.approx(0.15)
    assert one.recommended.rising == 1.0 and one.others.rising == 0.0
    assert three.others.mean_change == pytest.approx(0.0)
    assert seven.recommended.count == 0 and seven.others.mean_change == pytest.approx(0.10)
    assert (buy.points.recommended, buy.points.others) == (80, 10)  # day 9 is outside 7 days
    sell = record.views["verkaufen"]
    assert sell.horizons[0].recommended.mean_change == pytest.approx(-0.10)
    assert sell.snapshots == 1


async def test_horizons_in_the_future_are_not_counted(sessions):
    buys = buy_view(rec("b1", 70, 1_000_000, 1_000_000))
    await log_recommendations(sessions, LEAGUE, SNAPSHOT, buys, sell_view([], []))
    await seed_values(sessions, {"b1": {1: 1_100_000}})
    record = await track_record(sessions, LEAGUE, SNAPSHOT)  # same day: nothing is due yet
    assert not record.empty  # a snapshot exists, only the horizons are not complete yet
    assert record.views["kaufen"].horizons[0].recommended.count == 0
    assert record.views["kaufen"].points.count == 0


async def test_empty_record(sessions):
    record = await track_record(sessions, LEAGUE, SNAPSHOT)
    assert record.empty and record.first_day is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RecommendationLog)) == 0


def test_track_record_page(logged_in_client):
    html = logged_in_client.get("/empfehlungen/bilanz").text
    assert "<h1>Bilanz</h1>" in html
    assert 'href="/empfehlungen" aria-current="page">Plan<' in html
    assert "Noch zu wenig Daten" in html
    assert "Noch keine eigenen Gebote erfasst" in html
    assert "Kaufempfehlungen" in html and "Verkaufskandidaten" in html


def test_track_record_page_with_data(settings, logged_in_client):
    from sqlalchemy.orm import Session

    from app.db.models import MyOffer

    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(
            MyOffer(
                offer_id="1@1",
                league_id=LEAGUE,
                player_id="1",
                player_name="Max <Muster>",
                first_seen=now - timedelta(days=1),
                last_seen=now - timedelta(days=1),
                expires_at=now - timedelta(hours=20),
                amount=1_000_000,
                asking_price=900_000,
                market_value=900_000,
                outcome="lost",
                final_price=1_200_000,
                evaluated_at=now,
            )
        )
    engine.dispose()
    html = logged_in_client.get("/empfehlungen/bilanz").text
    assert "Max &lt;Muster&gt;" in html
    assert '<span class="badge badge--out">verloren</span>' in html
    assert "+20,0 %" in html  # lost by 20 %


async def test_snapshot_job(sessions):
    from zoneinfo import ZoneInfo

    from app.kickbase.league import LeagueContext
    from app.worker.jobs import JobContext, log_recommendation_snapshot
    from tests.fakes import (
        NOW,
        FakeClock,
        FakeOpenLigaDb,
        MemoryKV,
        add_recommendation_endpoints,
        fake_with_team_data,
        make_client,
        make_oldb_client,
    )

    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    clock = FakeClock(NOW)
    async with make_client(fake, clock=clock) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        kv = MemoryKV()
        ctx = JobContext(
            client=client,
            sessions=sessions,
            league=LeagueContext(client, kv, "Testliga"),
            kv=kv,
            tz=ZoneInfo("Europe/Berlin"),
            clock=clock,
            oldb=oldb,
        )
        summary = await log_recommendation_snapshot(ctx)
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(RecommendationLog))
        views = set((await session.execute(select(RecommendationLog.view))).scalars())
    assert summary == f"{count} Einträge protokolliert"
    assert count > 0 and views == {"kaufen", "verkaufen"}
