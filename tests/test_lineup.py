import pytest

from app.kickbase.models import Position
from app.services.lineup import FORMATIONS, SlotRange, infer_formation


@pytest.mark.parametrize("formation", FORMATIONS)
def test_complete_lineups_keep_their_formation(formation):
    assert infer_formation(*formation).label == "-".join(map(str, formation))


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((3, 5, 0), "3-5-2"),  # the real case: two forwards missing
        ((4, 4, 1), "4-4-2"),
        ((0, 0, 0), "3-4-3"),
        ((5, 0, 0), "5-3-2"),
        ((2, 6, 1), "3-6-1"),
    ],
)
def test_incomplete_lineups_use_nearest_formation(counts, expected):
    assert infer_formation(*counts).label == expected


def test_impossible_counts_are_shown_as_they_are():
    formation = infer_formation(6, 4, 0)
    assert formation.label == "6-4-0"
    assert formation.slots(Position.GOALKEEPER) == 1


def test_slots_pin_down_the_formation():
    # 4-4-2 with defender slot 4 empty: defence 1-3 + gap, midfield 5-8, attack 9-10.
    # 3-5-2 with midfield slot 4 empty has the same slots, so this stays a guess ...
    formation = infer_formation(
        3, 4, 2, defence=SlotRange(1, 3), midfield=SlotRange(5, 8), attack=SlotRange(9, 10)
    )
    assert formation.guessed
    # ... while a gap inside a line is unambiguous: midfield 4-8 with slot 6 empty.
    formation = infer_formation(
        3, 4, 2, defence=SlotRange(1, 3), midfield=SlotRange(4, 8), attack=SlotRange(9, 10)
    )
    assert formation.label == "3-5-2"
    assert not formation.guessed


def test_real_case_is_marked_as_guess():
    # 3 defenders (slots 1-3), 5 midfielders in 4-8, no forwards: 3-5-2 or 3-6-1
    formation = infer_formation(3, 5, 0, defence=SlotRange(1, 3), midfield=SlotRange(4, 8))
    assert formation.label == "3-5-2"
    assert formation.guessed


def test_complete_lineup_is_never_a_guess():
    assert not infer_formation(
        4, 4, 2, defence=SlotRange(1, 4), midfield=SlotRange(5, 8), attack=SlotRange(9, 10)
    ).guessed
    assert not infer_formation(4, 4, 2).guessed


def test_contradicting_slots_fall_back_to_counts(caplog):
    # a midfielder in slot 2 is impossible with at least three defenders
    formation = infer_formation(3, 5, 2, midfield=SlotRange(2, 8))
    assert formation.label == "3-5-2"
    assert "keiner erlaubten Formation" in caplog.text
