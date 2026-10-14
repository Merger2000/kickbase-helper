"""Pure unit tests for `app.services.matchday_history`.

Covers the `latest_placement_delta` property on `MatchdayHistory` – the dashboard
shows it as ▲N/▼N next to the latest matchday, and a gap in the stored history
(only days 1 and 3 are present) would otherwise compare unrelated rows and lie
about the ranking change.
"""

from app.services.matchday_history import MatchdayHistory, MatchdayHistoryEntry


def _entry(matchday: int, placement: int) -> MatchdayHistoryEntry:
    return MatchdayHistoryEntry(
        season=2026,
        matchday=matchday,
        points=50,
        placement=placement,
        matchday_placement=placement,
        team_value=180_000_000,
        missed_points=None,
    )


def test_empty_history_has_no_delta():
    history = MatchdayHistory(entries=(), latest=None)
    assert history.latest_placement_delta is None


def test_single_entry_has_no_delta():
    history = MatchdayHistory(entries=(_entry(1, 5),), latest=_entry(1, 5))
    assert history.latest_placement_delta is None


def test_consecutive_entries_return_placement_difference():
    history = MatchdayHistory(
        entries=(_entry(1, 5), _entry(2, 3)),
        latest=_entry(2, 3),
    )
    # Up by 2 (5 → 3).
    assert history.latest_placement_delta == 2


def test_gap_in_history_returns_no_delta():
    """Only matchdays 1 and 3 are stored: the previous matchday is 1, the latest is
    3, but they are not adjacent – the delta would be misleading."""
    history = MatchdayHistory(
        entries=(_entry(1, 5), _entry(3, 3)),
        latest=_entry(3, 3),
    )
    assert history.latest_placement_delta is None


def test_three_consecutive_entries_use_latest_pair():
    history = MatchdayHistory(
        entries=(_entry(1, 5), _entry(2, 4), _entry(3, 2)),
        latest=_entry(3, 2),
    )
    # Up by 2 (4 → 2), not 3 (5 → 2): the property compares the latest two rows.
    assert history.latest_placement_delta == 2
