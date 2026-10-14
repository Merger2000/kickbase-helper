"""Per-matchday report for the own team (Block C, C3.2): the post-matchday Telegram message.

Pure: `build_report` takes a parsed `UserTeamCenter`, a parsed `Ranking`, the user id, the
season/matchday numbers, a `player_id -> Position` mapping and the bench (with points) and
returns a `MatchdayReport` dataclass. No I/O, no DB access – the caller (the worker job in
C3.3) wires up the sources and decides what to do with the result (write the row, send the
Telegram message, etc.).

The bench source is the teamcenter's `nlp` when Kickbase returns it. When the teamcenter
arrives without a bench (current observation: this can happen for older finished matchdays),
the caller falls back to "current squad minus current lineup" with the points taken from
`matchday_points`; in that case the report is flagged with `bank_is_approximation=True`.

Placement delta: positive = moved up the table, negative = slid down, None on the first
matchday (no previous placement to compare). The own user's row is matched by id, not by
name – the fixtures anonymise every other manager and the test suite never relies on a
specific name.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.kickbase import models as m


class MatchdayReportError(Exception):
    """Base class for ``build_report`` errors."""


class UserNotInRanking(MatchdayReportError):
    def __init__(self, user_id: str) -> None:
        super().__init__(f"Eigener Manager {user_id} fehlt im Ranking")
        self.user_id = user_id


@dataclass(frozen=True)
class BenchEntry:
    """A bench player with the points they scored this matchday."""

    id: str
    name: str
    points: int
    position: int  # Position enum value (1=TW, 2=ABW, 3=MF, 4=ST)


@dataclass(frozen=True)
class LineupEntry:
    """A lineup player with the points they scored this matchday."""

    id: str
    name: str
    points: int
    position: int  # Position enum value


@dataclass(frozen=True)
class MissedSwap:
    """One bench player who would have scored more than the weakest lineup player of the same
    position. ``diff`` is the points that were left on the bench (bench − lineup)."""

    bench_id: str
    bench_name: str
    bench_points: int
    bench_position: int
    lineup_id: str
    lineup_name: str
    lineup_points: int
    diff: int


@dataclass(frozen=True)
class MatchdayReport:
    season: int
    matchday: int
    # Sum of points from the own lineup (Kickbase's `p` on the teamcenter).
    points: int
    # Season placement after this matchday (ranking's `spl`).
    placement: int
    # Placement before this matchday – None on the very first one.
    previous_placement: int | None
    # previous − current (positive = moved up).
    placement_delta: int | None
    # Placement within this single matchday (ranking's `mdpl`); None when unknown.
    matchday_placement: int | None
    # Sum of market values of the squad (ranking's `tv`).
    team_value: int
    # Top 3 lineup players by points (descending), ties broken by name.
    top_players: tuple[LineupEntry, ...]
    # Bottom 3 lineup players by points (ascending), ties broken by name.
    flop_players: tuple[LineupEntry, ...]
    # Sum of `diff` across all missed swaps.
    missed_points: int
    # Individual missed swaps, ordered by position then descending bench points.
    missed_swaps: tuple[MissedSwap, ...]
    # True when the bench came from "squad minus lineup" with points from `matchday_points`
    # because the teamcenter's `nlp` was empty.
    bank_is_approximation: bool


def build_report(
    *,
    teamcenter: m.UserTeamCenter,
    ranking: m.Ranking,
    user_id: str,
    season: int,
    matchday: int,
    positions: dict[str, int],
    bench: Iterable[BenchEntry],
    previous_placement: int | None,
    bank_is_approximation: bool,
) -> MatchdayReport:
    """Build the per-matchday report from already-fetched sources.

    `positions` covers every player appearing in the lineup or the bench – missing entries
    raise `MatchdayReportError` because the cross-position check (a swap is only allowed
    inside the same position) needs the position to be known."""
    own = ranking.for_user(user_id)
    if own is None:
        raise UserNotInRanking(user_id)

    lineup = _lineup(teamcenter, positions)
    bench_list = _bench(teamcenter, bench, positions)
    missed_swaps, missed_points = _missed_swaps(lineup, bench_list)

    return MatchdayReport(
        season=season,
        matchday=matchday,
        points=own.matchday_points,
        placement=own.season_placement,
        previous_placement=previous_placement,
        placement_delta=(
            previous_placement - own.season_placement if previous_placement is not None else None
        ),
        matchday_placement=own.matchday_placement,
        team_value=own.team_value,
        top_players=_top(lineup),
        flop_players=_flop(lineup),
        missed_points=missed_points,
        missed_swaps=missed_swaps,
        bank_is_approximation=bank_is_approximation,
    )


def _lineup(teamcenter: m.UserTeamCenter, positions: dict[str, int]) -> list[LineupEntry]:
    out: list[LineupEntry] = []
    for p in teamcenter.lineup:
        out.append(
            LineupEntry(
                id=p.id,
                name=p.name,
                points=p.points,
                position=_require_position(p.id, positions),
            )
        )
    return out


def _bench(
    teamcenter: m.UserTeamCenter,
    bench: Iterable[BenchEntry],
    positions: dict[str, int],
) -> list[BenchEntry]:
    # The bench from `teamcenter.bench` is always the preferred source (real data with the
    # right `st` participation code); the caller-supplied `bench` argument only fills in
    # when `teamcenter.bench` is empty (Kickbase has been observed to omit `nlp` on older
    # matchdays, see HANDOFF "Aktueller Stand").
    if teamcenter.bench:
        return [
            BenchEntry(
                id=p.id,
                name=p.name,
                points=p.points,
                position=_require_position(p.id, positions),
            )
            for p in teamcenter.bench
        ]
    return [
        BenchEntry(
            id=b.id,
            name=b.name,
            points=b.points,
            position=_require_position(b.id, positions),
        )
        for b in bench
    ]


def _require_position(player_id: str, positions: dict[str, int]) -> int:
    try:
        return positions[player_id]
    except KeyError as exc:
        raise MatchdayReportError(f"Position für Spieler {player_id} fehlt im Mapping") from exc


def _top(lineup: list[LineupEntry]) -> tuple[LineupEntry, ...]:
    ordered = sorted(lineup, key=lambda e: (-e.points, e.name))
    return tuple(ordered[:3])


def _flop(lineup: list[LineupEntry]) -> tuple[LineupEntry, ...]:
    ordered = sorted(lineup, key=lambda e: (e.points, e.name))
    return tuple(ordered[:3])


def _missed_swaps(
    lineup: list[LineupEntry], bench: list[BenchEntry]
) -> tuple[tuple[MissedSwap, ...], int]:
    """Greedy: for each position, repeatedly swap the weakest lineup player with the best
    bench player who would still beat them. The same lineup slot can only be swapped out
    once (a starter replaced by the bench stays in the lineup – the bench entry then would
    only re-enter if the next bench player also beat the new weakest)."""
    by_position_lineup: dict[int, list[LineupEntry]] = {}
    for entry in lineup:
        by_position_lineup.setdefault(entry.position, []).append(entry)

    swaps: list[MissedSwap] = []
    total = 0
    for position, starters in by_position_lineup.items():
        # Ascending by points, then by name for deterministic tie-breaking.
        starters_sorted = sorted(starters, key=lambda e: (e.points, e.name))
        candidates = [b for b in bench if b.position == position]
        # Descending by points, then by name for deterministic tie-breaking.
        candidates_sorted = sorted(candidates, key=lambda e: (-e.points, e.name))
        available = list(starters_sorted)
        for bench_entry in candidates_sorted:
            if not available:
                break
            # The lineup player available with the smallest `points` (the first one).
            weakest = available[0]
            if bench_entry.points <= weakest.points:
                # The bench is sorted descending; nothing further can beat this starter.
                break
            diff = bench_entry.points - weakest.points
            swaps.append(
                MissedSwap(
                    bench_id=bench_entry.id,
                    bench_name=bench_entry.name,
                    bench_points=bench_entry.points,
                    bench_position=bench_entry.position,
                    lineup_id=weakest.id,
                    lineup_name=weakest.name,
                    lineup_points=weakest.points,
                    diff=diff,
                )
            )
            total += diff
            # Replace the weakest starter with the bench player (he stays in the lineup
            # because he scored more), and remove him from the bench.
            available = [*available[1:], bench_entry]
            available.sort(key=lambda e: (e.points, e.name))

    swaps.sort(key=lambda s: (s.bench_position, -s.bench_points, s.bench_name))
    return tuple(swaps), total
