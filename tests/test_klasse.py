from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.db.models import Base, MatchdayPoints, Player
from app.db.session import make_engine, make_sessionmaker
from app.services.klasse import load_class

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'klasse.db'}"
    sync = create_engine(url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def seed(sessions):
    async with sessions.begin() as session:
        for pid, pos, value, updated in (
            ("star", 3, 30_000_000, NOW),
            ("mid", 3, 10_000_000, NOW),
            ("cheap", 3, 1_000_000, NOW),
            ("gone", 3, 50_000_000, NOW - timedelta(days=30)),  # left the Bundesliga
            ("keeper", 1, 5_000_000, NOW),
        ):
            session.add(
                Player(
                    id=pid,
                    name=pid,
                    team_id="1",
                    position=pos,
                    status=0,
                    market_value=value,
                    image=None,
                    updated_at=updated,
                )
            )
        # "cheap" scored best this season, "star" played 4 matches, "mid" none.
        for md, (pid, points) in enumerate(
            [("star", 150), ("star", 170), ("star", 160), ("star", 180), ("cheap", 300)], 1
        ):
            session.add(
                MatchdayPoints(season=2026, matchday=md, player_id=pid, points=points, team_id="1")
            )


async def test_class_combines_market_value_rank_and_season_average(sessions):
    await seed(sessions)
    table = await load_class(sessions, 2026, NOW - timedelta(days=2), 0.5, 8)
    assert "gone" not in table  # only players still in the Bundesliga
    star, mid, cheap = table["star"], table["mid"], table["cheap"]
    assert star.mv_percentile == 1.0 and cheap.mv_percentile == 0.0  # within the midfield
    assert table["keeper"].mv_percentile == 0.5  # alone in his position: neutral
    # 4 appearances of 8 → the season average counts 25 %, one appearance 6.25 %.
    assert star.season_share == pytest.approx(0.25) and star.average == pytest.approx(165)
    assert cheap.season_share == pytest.approx(0.0625)
    assert mid.season_share == 0 and mid.average is None
    assert star.value == pytest.approx(0.75 * 1.0 + 0.25 * 0.0)  # cheap has the better Ø
    assert cheap.value == pytest.approx(0.9375 * 0.0 + 0.0625 * 1.0)


async def test_without_a_season_only_the_market_value_counts(sessions):
    await seed(sessions)
    table = await load_class(sessions, None, NOW - timedelta(days=2), 0.5, 8)
    assert table["star"].value == 1.0 and table["star"].season_share == 0
