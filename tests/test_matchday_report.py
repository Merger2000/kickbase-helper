"""Per-matchday report tests (Block C, C3.2).

The report builder is a pure function: every test crafts a `UserTeamCenter` (lineup +
optional bench) and a `Ranking` (placements, points, team value), hands them to
``build_report`` and checks the dataclass. Fixture-driven tests parse the real responses
under ``tests/fixtures/kickbase/`` and check the wiring; the targeted cases below exercise
each acceptance criterion in the checkbox.
"""

import pytest

from app.kickbase import models as m
from app.services.matchday_report import (
    BenchEntry,
    MatchdayReport,
    UserNotInRanking,
    build_report,
)
from tests.fakes import fixture

USER_ID = "9000001"  # "Manager 1" – the requester in every fixture
SEASON = 2026
MATCHDAY = 5


def _teamcenter() -> m.UserTeamCenter:
    return m.UserTeamCenter.model_validate(fixture("league_user_teamcenter"))


def _ranking() -> m.Ranking:
    return m.Ranking.model_validate(fixture("league_ranking_day"))


def _positions(teamcenter: m.UserTeamCenter) -> dict[str, int]:
    """Positions are passed in (no I/O); the teamcenter only carries player ids, so we hand
    each player the position Kickbase would have returned. The values match `Position`."""
    mapping = {
        "3204": m.Position.MIDFIELDER.value,
        "3470": m.Position.DEFENDER.value,
        "3759": m.Position.DEFENDER.value,
        "6061": m.Position.DEFENDER.value,
        "7737": m.Position.MIDFIELDER.value,
        "8227": m.Position.GOALKEEPER.value,
        "8307": m.Position.MIDFIELDER.value,
        "10120": m.Position.FORWARD.value,
        "11956": m.Position.MIDFIELDER.value,
        "17285": m.Position.DEFENDER.value,
        "17288": m.Position.MIDFIELDER.value,
    }
    # Belt-and-braces: every lineup player must be in the mapping.
    for p in teamcenter.lineup:
        assert p.id in mapping, f"Test fixture missing position for lineup player {p.id}"
    return mapping


def _own_summary(ranking: m.Ranking) -> m.RankingUser:
    own = ranking.for_user(USER_ID)
    assert own is not None
    return own


# ---------------------------------------------------------------------------- fixture-driven


def test_fixture_driven_report_matches_ranking_for_own_user():
    teamcenter = _teamcenter()
    report = build_report(
        teamcenter=teamcenter,
        ranking=_ranking(),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions=_positions(teamcenter),
        bench=[],
        previous_placement=3,  # arbitrary: checks the delta arithmetic only
        bank_is_approximation=True,
    )
    own = _own_summary(_ranking())
    assert report.points == own.matchday_points == 934
    assert report.placement == own.season_placement == 2
    assert report.matchday_placement == own.matchday_placement == 2
    assert report.team_value == own.team_value == 182_839_707
    assert report.placement_delta == 3 - own.season_placement
    assert report.bank_is_approximation is True
    assert report.missed_points == 0  # bench was empty in the fixture
    assert report.missed_swaps == ()


def test_fixture_driven_top_and_flop_are_sorted_by_points():
    teamcenter = _teamcenter()
    report = build_report(
        teamcenter=teamcenter,
        ranking=_ranking(),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions=_positions(teamcenter),
        bench=[],
        previous_placement=None,
        bank_is_approximation=False,
    )
    # Lineup points in the fixture, sorted: 311, 335, 205, 125, 88, 57, 55, 47, 43, 24, 22.
    assert [p.points for p in report.top_players] == [335, 311, 205]
    assert [p.points for p in report.flop_players] == [22, 24, 43]
    # Tie-break by name when the points are equal – none in the fixture, but the contract is
    # that the names are alphabetical within a tie.
    for run in (report.top_players, report.flop_players):
        points = [p.points for p in run]
        assert points == sorted(points, reverse=run is report.top_players)


# ---------------------------------------------------------------------------- placement delta


@pytest.mark.parametrize(
    ("previous", "expected_delta"),
    [
        (3, 1),  # went up (3 -> 2, delta = +1)
        (2, 0),  # same
        (1, -1),  # went down (1 -> 2, delta = -1)
    ],
)
def test_placement_delta_signs(previous: int, expected_delta: int):
    report = build_report(
        teamcenter=_teamcenter(),
        ranking=_ranking(),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions=_positions(_teamcenter()),
        bench=[],
        previous_placement=previous,
        bank_is_approximation=False,
    )
    assert report.placement_delta == expected_delta
    assert report.previous_placement == previous


def test_first_matchday_has_no_previous_placement():
    report = build_report(
        teamcenter=_teamcenter(),
        ranking=_ranking(),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions=_positions(_teamcenter()),
        bench=[],
        previous_placement=None,
        bank_is_approximation=False,
    )
    assert report.previous_placement is None
    assert report.placement_delta is None


def test_unknown_user_in_ranking_raises():
    with pytest.raises(UserNotInRanking):
        build_report(
            teamcenter=_teamcenter(),
            ranking=_ranking(),
            user_id="9999999",
            season=SEASON,
            matchday=MATCHDAY,
            positions=_positions(_teamcenter()),
            bench=[],
            previous_placement=2,
            bank_is_approximation=False,
        )


# ---------------------------------------------------------------------------- missed swaps


def _swap_teamcenter_and_ranking(
    *,
    lineup: list[dict],
    bench: list[dict],
    positions: dict[str, int],
    matchday_points: int,
    season_placement: int,
    matchday_placement: int,
    previous_placement: int | None,
) -> MatchdayReport:
    tc_raw = {
        "n": "Manager 1",
        "lp": lineup,
        "nlp": bench,
        "ppc": 0,
        "clpc": len(lineup),
        "us": [],
    }
    ranking_raw = {
        "ti": "Testliga",
        "us": [
            {
                "i": USER_ID,
                "n": "Manager 1",
                "sp": 1,
                "mdp": matchday_points,
                "tv": 100_000_000,
                "spl": season_placement,
                "mdpl": matchday_placement,
            },
            {
                "i": "9000002",
                "n": "Manager 2",
                "sp": 2,
                "mdp": 0,
                "tv": 100_000_000,
                "spl": 2,
                "mdpl": 2,
            },
        ],
        "day": MATCHDAY,
    }
    return build_report(
        teamcenter=m.UserTeamCenter.model_validate(tc_raw),
        ranking=m.Ranking.model_validate(ranking_raw),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions=positions,
        bench=[],
        previous_placement=previous_placement,
        bank_is_approximation=False,
    )


def _lineup_player(*, id_: str, name: str, points: int, position: int, mi: int = 1) -> dict:
    return {
        "i": id_,
        "n": name,
        "tid": 1,
        "p": points,
        "st": 5,
        "mi": mi,
        "mt": 90,
        "mtd": "90",
        "md": "2026-10-10T13:30:00Z",
        "mst": 2,
        "k": [],
        "pim": None,
        "ictp": False,
    }


def _bench_player(*, id_: str, name: str, points: int, mi: int | None = None) -> dict:
    entry: dict = {
        "i": id_,
        "n": name,
        "tid": 1,
        "p": points,
        "st": 3,
        "k": [],
        "pim": None,
        "ictp": False,
    }
    if mi is not None:
        entry.update({"mi": mi, "mt": 90, "mtd": "90", "mst": 2})
    return entry


def test_no_missed_swaps_when_bench_never_beats_weakest_lineup():
    """One defender on the bench with 30 points, the weakest defender in the lineup has 50:
    the bench cannot beat anyone, no swap, no missed points."""
    positions = {"lp1": 2, "bp1": 2}
    lineup = [_lineup_player(id_="lp1", name="Starter", points=50, position=2)]
    bench = [_bench_player(id_="bp1", name="Bench", points=30)]
    report = _swap_teamcenter_and_ranking(
        lineup=lineup,
        bench=bench,
        positions=positions,
        matchday_points=50,
        season_placement=1,
        matchday_placement=1,
        previous_placement=None,
    )
    assert report.missed_points == 0
    assert report.missed_swaps == ()


def test_missed_swap_uses_weakest_lineup_at_same_position():
    """Two defenders on the bench (40 and 35), the weakest starter on the same position has
    20. Only the 40-point bench beats him (35 < 50, the other starter)."""
    positions = {"lp1": 2, "lp2": 2, "bp-strong": 2, "bp-weak": 2}
    lineup = [
        _lineup_player(id_="lp1", name="A", points=50, position=2),
        _lineup_player(id_="lp2", name="B", points=20, position=2),
    ]
    bench = [
        _bench_player(id_="bp-strong", name="StrongBench", points=40),
        _bench_player(id_="bp-weak", name="WeakBench", points=35),
    ]
    report = _swap_teamcenter_and_ranking(
        lineup=lineup,
        bench=bench,
        positions=positions,
        matchday_points=70,
        season_placement=1,
        matchday_placement=1,
        previous_placement=None,
    )
    assert [(s.bench_id, s.lineup_id, s.diff) for s in report.missed_swaps] == [
        ("bp-strong", "lp2", 20),
    ]
    assert report.missed_points == 20


def test_swap_does_not_cross_positions():
    """A bench forward with 100 points must not swap with a defender who scored only 10;
    swaps are only inside the same position."""
    positions = {"lp-def": 2, "lp-mid": 3, "bp-mid": 3, "bp-fwd": 4}
    lineup = [
        _lineup_player(id_="lp-def", name="DefStarter", points=10, position=2),
        _lineup_player(id_="lp-mid", name="MidStarter", points=20, position=3),
    ]
    bench = [
        _bench_player(id_="bp-mid", name="MidBench", points=50),
        _bench_player(id_="bp-fwd", name="FwdBench", points=100),
    ]
    report = _swap_teamcenter_and_ranking(
        lineup=lineup,
        bench=bench,
        positions=positions,
        matchday_points=30,
        season_placement=1,
        matchday_placement=1,
        previous_placement=None,
    )
    # Only the midfielder-midfielder swap counts.
    assert [(s.bench_id, s.lineup_id, s.diff) for s in report.missed_swaps] == [
        ("bp-mid", "lp-mid", 30),
    ]
    assert report.missed_points == 30


def test_player_with_zero_points_is_treated_as_zero():
    """A lineup or bench player who didn't play (points=0) is still considered; with 0
    points he can never beat anyone, but his presence must not break the algorithm."""
    positions = {"lp1": 2, "lp2": 2, "lp3": 2, "bp1": 2, "bp2": 2}
    lineup = [
        _lineup_player(id_="lp1", name="A", points=100, position=2),
        _lineup_player(id_="lp2", name="B", points=0, position=2),
        _lineup_player(id_="lp3", name="C", points=50, position=2),
    ]
    bench = [
        _bench_player(id_="bp1", name="BenchA", points=0),
        _bench_player(id_="bp2", name="BenchB", points=60),
    ]
    report = _swap_teamcenter_and_ranking(
        lineup=lineup,
        bench=bench,
        positions=positions,
        matchday_points=150,
        season_placement=1,
        matchday_placement=1,
        previous_placement=None,
    )
    # BenchB (60) beats lp2 (0) by 60; BenchA (0) beats nobody.
    assert [(s.bench_id, s.lineup_id, s.diff) for s in report.missed_swaps] == [
        ("bp2", "lp2", 60),
    ]
    assert report.missed_points == 60


def test_greedy_swap_keeps_looking_after_each_replacement():
    """The algorithm is greedy: the strongest bench candidate swaps with the weakest lineup
    first, then the next-strongest bench candidate looks at the updated lineup. Two swaps
    happen iff the second bench candidate still beats the new weakest.

    Bench (descending): 40, 30, 25. Lineup (ascending): 50, 20, 15.
      - 40 > 15: swap, diff 25. Lineup becomes 50, 20, 40.
      - 30 > 20: swap, diff 10. Lineup becomes 50, 30, 40.
      - 25 < 30: stop."""
    positions = {"lp1": 2, "lp2": 2, "lp3": 2, "bp1": 2, "bp2": 2, "bp3": 2}
    lineup = [
        _lineup_player(id_="lp1", name="A", points=50, position=2),
        _lineup_player(id_="lp2", name="B", points=20, position=2),
        _lineup_player(id_="lp3", name="C", points=15, position=2),
    ]
    bench = [
        _bench_player(id_="bp1", name="Bench1", points=40),
        _bench_player(id_="bp2", name="Bench2", points=30),
        _bench_player(id_="bp3", name="Bench3", points=25),
    ]
    report = _swap_teamcenter_and_ranking(
        lineup=lineup,
        bench=bench,
        positions=positions,
        matchday_points=85,
        season_placement=1,
        matchday_placement=1,
        previous_placement=None,
    )
    assert [(s.bench_id, s.lineup_id, s.diff) for s in report.missed_swaps] == [
        ("bp1", "lp3", 25),
        ("bp2", "lp2", 10),
    ]
    assert report.missed_points == 35


def test_position_missing_from_mapping_raises():
    positions = {"lp1": 2}  # bp1 missing
    lineup = [_lineup_player(id_="lp1", name="A", points=50, position=2)]
    bench = [_bench_player(id_="bp1", name="B", points=10)]
    with pytest.raises(Exception, match="Position"):
        _swap_teamcenter_and_ranking(
            lineup=lineup,
            bench=bench,
            positions=positions,
            matchday_points=50,
            season_placement=1,
            matchday_placement=1,
            previous_placement=None,
        )


# ---------------------------------------------------------------------------- approximation


def test_bench_from_caller_is_used_when_teamcenter_nlp_is_empty():
    """Kickbase has been observed to return an empty `nlp` for older matchdays. The caller
    fills in `bench` from "squad minus lineup" with points from `matchday_points`; the
    report must flag this as an approximation and use those points."""
    tc_raw = {
        "n": "Manager 1",
        "lp": [_lineup_player(id_="lp1", name="A", points=10, position=2)],
        "nlp": [],  # Kickbase didn't return the bench
        "ppc": 0,
        "clpc": 1,
        "us": [],
    }
    ranking_raw = {
        "ti": "Testliga",
        "us": [
            {
                "i": USER_ID,
                "n": "Manager 1",
                "sp": 1,
                "mdp": 10,
                "tv": 100_000_000,
                "spl": 1,
                "mdpl": 1,
            },
            {
                "i": "9000002",
                "n": "Manager 2",
                "sp": 2,
                "mdp": 0,
                "tv": 100_000_000,
                "spl": 2,
                "mdpl": 2,
            },
        ],
        "day": MATCHDAY,
    }
    bench_entries = [
        BenchEntry(id="bp1", name="B", points=80, position=2),
    ]
    report = build_report(
        teamcenter=m.UserTeamCenter.model_validate(tc_raw),
        ranking=m.Ranking.model_validate(ranking_raw),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions={"lp1": 2, "bp1": 2},
        bench=bench_entries,
        previous_placement=None,
        bank_is_approximation=True,
    )
    assert report.bank_is_approximation is True
    assert report.missed_points == 70  # 80 (bench) − 10 (weakest lineup)
    assert [(s.bench_id, s.bench_points, s.lineup_points, s.diff) for s in report.missed_swaps] == [
        ("bp1", 80, 10, 70),
    ]


def test_real_nlp_overrides_caller_bench():
    """When the teamcenter returns a non-empty `nlp`, those points win. The caller-supplied
    `bench` is ignored (the caller should not supply one in that case)."""
    tc_raw = {
        "n": "Manager 1",
        "lp": [_lineup_player(id_="lp1", name="A", points=10, position=2)],
        "nlp": [_bench_player(id_="bp-real", name="RealBench", points=30)],
        "ppc": 0,
        "clpc": 1,
        "us": [],
    }
    ranking_raw = {
        "ti": "Testliga",
        "us": [
            {
                "i": USER_ID,
                "n": "Manager 1",
                "sp": 1,
                "mdp": 10,
                "tv": 100_000_000,
                "spl": 1,
                "mdpl": 1,
            },
            {
                "i": "9000002",
                "n": "Manager 2",
                "sp": 2,
                "mdp": 0,
                "tv": 100_000_000,
                "spl": 2,
                "mdpl": 2,
            },
        ],
        "day": MATCHDAY,
    }
    caller_bench = [BenchEntry(id="bp-other", name="StaleBench", points=999, position=2)]
    report = build_report(
        teamcenter=m.UserTeamCenter.model_validate(tc_raw),
        ranking=m.Ranking.model_validate(ranking_raw),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions={"lp1": 2, "bp-real": 2},
        bench=caller_bench,
        previous_placement=None,
        bank_is_approximation=False,
    )
    assert report.bank_is_approximation is False
    assert [(s.bench_id, s.bench_points) for s in report.missed_swaps] == [("bp-real", 30)]
    assert report.missed_points == 20  # 30 (real nlp) − 10 (weakest lineup)
