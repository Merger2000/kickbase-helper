"""Formation inference for the pitch view."""

import logging
from dataclasses import dataclass

from app.kickbase.models import Position

logger = logging.getLogger(__name__)

# Formations Kickbase allows (defenders, midfielders, forwards).
FORMATIONS: tuple[tuple[int, int, int], ...] = (
    (3, 4, 3),
    (3, 5, 2),
    (3, 6, 1),
    (4, 2, 4),
    (4, 3, 3),
    (4, 4, 2),
    (4, 5, 1),
    (5, 3, 2),
    (5, 4, 1),
)


@dataclass(frozen=True)
class Formation:
    defenders: int
    midfielders: int
    forwards: int
    # True when empty slots make the formation ambiguous; the UI says "vermutet".
    guessed: bool = False

    @property
    def label(self) -> str:
        return f"{self.defenders}-{self.midfielders}-{self.forwards}"

    def slots(self, position: Position) -> int:
        return {
            Position.GOALKEEPER: 1,
            Position.DEFENDER: self.defenders,
            Position.MIDFIELDER: self.midfielders,
            Position.FORWARD: self.forwards,
        }[position]


@dataclass(frozen=True)
class SlotRange:
    """Lowest and highest occupied lineup slot of one line (None if the line is empty)."""

    low: int | None = None
    high: int | None = None


NO_SLOTS = SlotRange()


def infer_formation(
    defenders: int,
    midfielders: int,
    forwards: int,
    *,
    defence: SlotRange = NO_SLOTS,
    midfield: SlotRange = NO_SLOTS,
    attack: SlotRange = NO_SLOTS,
) -> Formation:
    """Formation of the lineup.

    Kickbase numbers lineup slots 0 (goalkeeper) to 10 line by line, defence first. Occupied
    slots therefore bound the line sizes even when some slots are empty: with D defenders and
    M midfielders, defenders sit in 1..D, midfielders in D+1..D+M, forwards above. Among the
    allowed formations consistent with the counts and slots, the one needing the fewest
    additional players wins (ties: listed order); it is marked as guessed if several fit.
    """

    def fits(f: tuple[int, int, int]) -> bool:
        d, mid, _ = f
        if f[0] < defenders or f[1] < midfielders or f[2] < forwards:
            return False
        if defence.high is not None and defence.high > d:
            return False
        if midfield.low is not None and midfield.low < d + 1:
            return False
        if midfield.high is not None and midfield.high > d + mid:
            return False
        return attack.low is None or attack.low >= d + mid + 1

    candidates = [f for f in FORMATIONS if fits(f)]
    if not candidates:
        logger.warning(
            "Aufstellung passt zu keiner erlaubten Formation: %d-%d-%d (Slots %s %s %s)",
            defenders,
            midfielders,
            forwards,
            defence,
            midfield,
            attack,
        )
        return Formation(defenders, midfielders, forwards)
    best = min(
        candidates,
        key=lambda f: (f[0] - defenders) + (f[1] - midfielders) + (f[2] - forwards),
    )
    return Formation(*best, guessed=len(candidates) > 1)
