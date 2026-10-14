"""Per-matchday Telegram message tests (Block C, C3.3).

The message composer is a pure function: every test crafts a ``MatchdayReport`` (either
directly or via ``build_report``) and asserts on the resulting HTML string. The
table-driven cases cover the placement-delta labels and the approximation marker; the
targeted cases check the player links, HTML escaping and the "no other manager names"
invariant. No Kickbase / DB I/O happens here – the worker job wires the sources in C3.4.
"""

import pytest

from app.kickbase import models as m
from app.notify.matchday_report import compose_message
from app.services.matchday_report import (
    LineupEntry,
    MatchdayReport,
    MissedSwap,
    build_report,
)
from tests.test_matchday_report import _bench_player, _lineup_player

USER_ID = "9000001"
SEASON = 2026
MATCHDAY = 5
BASE = "https://kickbase.example.com"


def _report(
    *,
    placement: int = 3,
    previous_placement: int | None = 2,
    matchday_placement: int | None = None,
    points: int = 50,
    top_players: tuple[LineupEntry, ...] = (),
    flop_players: tuple[LineupEntry, ...] = (),
    missed_points: int = 0,
    missed_swaps: tuple[MissedSwap, ...] = (),
    bank_is_approximation: bool = False,
) -> MatchdayReport:
    return MatchdayReport(
        season=SEASON,
        matchday=MATCHDAY,
        points=points,
        placement=placement,
        previous_placement=previous_placement,
        placement_delta=(
            previous_placement - placement if previous_placement is not None else None
        ),
        matchday_placement=matchday_placement,
        team_value=100_000_000,
        top_players=top_players,
        flop_players=flop_players,
        missed_points=missed_points,
        missed_swaps=missed_swaps,
        bank_is_approximation=bank_is_approximation,
    )


def _player(*, id_: str, name: str, points: int, position: int = 3) -> LineupEntry:
    return LineupEntry(id=id_, name=name, points=points, position=position)


def _swap(
    *,
    bench_id="b1",
    bench_name="Bank",
    bench_points=80,
    lineup_id="l1",
    lineup_name="Start",
    lineup_points=10,
    diff=70,
    position=3,
) -> MissedSwap:
    return MissedSwap(
        bench_id=bench_id,
        bench_name=bench_name,
        bench_points=bench_points,
        bench_position=position,
        lineup_id=lineup_id,
        lineup_name=lineup_name,
        lineup_points=lineup_points,
        diff=diff,
    )


# ------------------------------------------------------- placement-delta labels


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (5, 2, "Platz 2 (▲3)"),  # went up by 3
        (5, 4, "Platz 4 (▲1)"),  # went up by 1
        (3, 3, "Platz 3 (±0)"),  # unchanged
        (1, 2, "Platz 2 (▼1)"),  # went down by 1
        (1, 4, "Platz 4 (▼3)"),  # went down by 3
    ],
)
def test_placement_change_label(previous: int, current: int, expected: str) -> None:
    report = _report(placement=current, previous_placement=previous)
    msg = compose_message("Testliga", report, BASE)
    assert expected in msg


def test_first_matchday_shows_first_matchday_label() -> None:
    """1. Spieltag ohne Vorwert – the header carries the fixed label, not a delta arrow."""
    report = _report(placement=3, previous_placement=None)
    msg = compose_message("Testliga", report, BASE)
    header = msg.split("\n")[1]
    assert "Platz 3 – 1. Spieltag" in header
    assert "▲" not in header
    assert "▼" not in header
    assert "±0" not in header


# ------------------------------------------------------- top / flop with links


def test_top_players_are_linked_with_points() -> None:
    report = _report(
        top_players=(
            _player(id_="1", name="Müller", points=311),
            _player(id_="2", name="Wirtz", points=205),
        ),
    )
    msg = compose_message("Testliga", report, BASE)
    assert f'<a href="{BASE}/spieler/1">Müller</a> 311P' in msg
    assert f'<a href="{BASE}/spieler/2">Wirtz</a> 205P' in msg
    assert "🏆 Top:" in msg


def test_flop_players_are_linked_with_points() -> None:
    report = _report(
        flop_players=(
            _player(id_="11", name="Beier", points=22),
            _player(id_="12", name="Tah", points=24),
        ),
    )
    msg = compose_message("Testliga", report, BASE)
    assert f'<a href="{BASE}/spieler/11">Beier</a> 22P' in msg
    assert f'<a href="{BASE}/spieler/12">Tah</a> 24P' in msg
    assert "💤 Flop:" in msg


def test_no_missed_section_when_no_swaps() -> None:
    report = _report(missed_points=0, missed_swaps=())
    msg = compose_message("Testliga", report, BASE)
    assert "Verpasste" not in msg
    assert "Näherung" not in msg


# ------------------------------------------------------- approximation marker


@pytest.mark.parametrize(
    ("bank_is_approximation", "expected_line"),
    [
        (False, "❌ Verpasste Punkte: 70"),
        (True, "❌ Verpasste Punkte: 70 (Näherung, Bank aus Kader)"),
    ],
)
def test_missed_points_marker(bank_is_approximation: bool, expected_line: str) -> None:
    swap = _swap()
    report = _report(
        missed_points=70,
        missed_swaps=(swap,),
        bank_is_approximation=bank_is_approximation,
    )
    msg = compose_message("Testliga", report, BASE)
    assert expected_line in msg


def test_missed_swaps_are_formatted_with_links_and_diff() -> None:
    """The detail line lists every swap as `bench → lineup (lineup_points → +diff)`."""
    swaps = (
        _swap(bench_id="b1", bench_name="Bank1", bench_points=80, diff=70),
        _swap(
            bench_id="b2",
            bench_name="Bank2",
            bench_points=40,
            lineup_id="l2",
            lineup_name="Start2",
            lineup_points=15,
            diff=25,
        ),
    )
    report = _report(missed_points=95, missed_swaps=swaps)
    msg = compose_message("Testliga", report, BASE)
    assert (
        f'<a href="{BASE}/spieler/b1">Bank1</a> → <a href="{BASE}/spieler/l1">Start</a> (10 → +70)'
    ) in msg
    assert (
        f'<a href="{BASE}/spieler/b2">Bank2</a> → <a href="{BASE}/spieler/l2">Start2</a> (15 → +25)'
    ) in msg


# ------------------------------------------------------- HTML escaping


def test_player_names_are_html_escaped() -> None:
    report = _report(
        top_players=(_player(id_="1", name="<b>Müller</b>", points=100),),
    )
    msg = compose_message("Testliga", report, BASE)
    assert "<b>Müller</b>" not in msg
    assert "&lt;b&gt;Müller&lt;/b&gt;" in msg


def test_league_name_is_html_escaped() -> None:
    report = _report()
    msg = compose_message("Böse <Liga>", report, BASE)
    assert "Böse &lt;Liga&gt;" in msg
    assert "Böse <Liga>" not in msg


# ------------------------------------------------------- no foreign manager names


def test_message_contains_no_foreign_manager_names() -> None:
    """Even with other managers in the ranking, only own-team data ends up in the message."""
    tc_raw = {
        "n": "Manager 1",
        "lp": [_lineup_player(id_="lp1", name="OwnStarter", points=10, position=3)],
        "nlp": [_bench_player(id_="bp1", name="OwnBench", points=80)],
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
                "n": "Foreign Manager A",
                "sp": 2,
                "mdp": 0,
                "tv": 100_000_000,
                "spl": 2,
                "mdpl": 2,
            },
            {
                "i": "9000003",
                "n": "Anderer Manager",
                "sp": 3,
                "mdp": 0,
                "tv": 100_000_000,
                "spl": 3,
                "mdpl": 3,
            },
        ],
        "day": MATCHDAY,
    }
    report = build_report(
        teamcenter=m.UserTeamCenter.model_validate(tc_raw),
        ranking=m.Ranking.model_validate(ranking_raw),
        user_id=USER_ID,
        season=SEASON,
        matchday=MATCHDAY,
        positions={"lp1": 3, "bp1": 3},
        bench=[],
        previous_placement=None,
        bank_is_approximation=False,
    )
    msg = compose_message("Testliga", report, BASE)
    for foreign in ("Foreign Manager A", "Anderer Manager", "9000002", "9000003"):
        assert foreign not in msg, f"fremder Manager sickert durch: {foreign!r}"
