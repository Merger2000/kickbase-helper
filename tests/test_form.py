import pytest
from sqlalchemy import create_engine

from app.db.models import Base, MatchdayPoints, Player
from app.db.session import make_engine, make_sessionmaker
from app.services.form import load_form, percentile_ranks
from tests.fakes import NOW


def test_percentile_ranks():
    ranks = percentile_ranks({"a": 10, "b": 20, "c": 20, "d": 40})
    # strictly better than: ties share the lower rank
    assert ranks == {"a": 0.0, "b": pytest.approx(1 / 3), "c": pytest.approx(1 / 3), "d": 1.0}
    assert percentile_ranks({"only": 5}) == {"only": 0.5}


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'form.db'}"
    sync = create_engine(url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def seed(sessions, points: dict[str, dict[int, int]], positions: dict[str, int]):
    async with sessions.begin() as session:
        for pid, position in positions.items():
            session.add(
                Player(
                    id=pid,
                    name=pid,
                    team_id="1",
                    position=position,
                    status=0,
                    market_value=1,
                    image=None,
                    updated_at=NOW,
                )
            )
        for pid, by_day in points.items():
            for day, value in by_day.items():
                session.add(
                    MatchdayPoints(
                        season=2026,
                        matchday=day,
                        player_id=pid,
                        points=value,
                        team_id="1",
                        opponent_id="2",
                        is_home=True,
                        kickoff=NOW,
                    )
                )


async def test_recency_weights_and_percentiles(sessions):
    await seed(
        sessions,
        {"new_star": {3: 120}, "old_star": {1: 120}, "steady": {1: 50, 2: 50, 3: 50}},
        {"new_star": 3, "old_star": 3, "steady": 3, "keeper": 1},
    )
    table = await load_form(sessions, 2026, {1: 1, 2: 1, 3: 1, 4: 1}, [3, 2, 1])
    assert table.matchdays == (3, 2, 1)  # matchday 4 finished but not synced yet
    assert table.incomplete == (4,)
    new, old, steady = (table.by_player[p] for p in ("new_star", "old_star", "steady"))
    assert new.weighted_points == pytest.approx(360 / 6)  # recent matchday counts 3x
    assert old.weighted_points == pytest.approx(120 / 6)
    assert steady.weighted_points == pytest.approx(50)
    assert (new.percentile, steady.percentile, old.percentile) == (1.0, 0.5, 0.0)
    assert (new.appearances, old.appearances, steady.appearances) == (1, 1, 3)
    assert table.by_player["keeper"].percentile == 0.5  # alone in his position


async def test_no_points_yet(sessions):
    await seed(sessions, {}, {"a": 2})
    table = await load_form(sessions, 2026, {1: 2, 2: 2}, [5, 4, 3, 2, 1])
    assert table.by_player == {} and table.matchdays == ()


async def test_partially_synced_matchday_is_left_out(sessions):
    # matchday 2: two clubs played, only one is stored so far (worker saves match by match)
    await seed(sessions, {"a": {1: 10, 2: 99}, "b": {1: 20}}, {"a": 3, "b": 3})
    table = await load_form(sessions, 2026, {1: 1, 2: 2}, [2, 1])
    assert table.matchdays == (1,)
    assert table.incomplete == (2,)
    assert table.by_player["a"].weighted_points == 10
