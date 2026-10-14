"""Verkaufs-Guide: wie man aus einem negativen Budget herauskommt.

In Kickbase gibt es im Minus keine Punkte und jeder fehlende Spieler in der Startelf kostet
−100 Punkte. Der Guide zeigt, wie viel man verkaufen muss und nennt Wege dorthin:

- **geringster Punkteverlust** (die Empfehlung): die Kombination aus bis zu `max_players`
  Spielern, die das Minus deckt und dabei am wenigsten Punkte kostet. Verlust eines Spielers =
  Saison-Ø (seine Klasse) und erwartete Punkte am nächsten Spieltag (Ø × Startchance), dazu
  `empty_slot_points` für jeden Platz, der danach in der Elf leer bliebe. Gewichte in
  `config/scoring.yaml` (`sell_guide`).
- **wenigste Verkäufe**: die wertvollsten zuerst – schnell, verkauft aber oft den besten Spieler.
- **nur Verkaufskandidaten**: die Spieler der Verkaufsliste (reicht oft nicht).
"""

from dataclasses import dataclass
from itertools import combinations

from app.kickbase import models as m
from app.services.lineup import FORMATIONS, Formation

POSITIONS = (m.Position.GOALKEEPER, m.Position.DEFENDER, m.Position.MIDFIELDER, m.Position.FORWARD)


@dataclass(frozen=True)
class GuideWeights:
    season: float = 0.6  # weight of the season average (the player's class)
    matchday: float = 0.4  # weight of the expected points next matchday (Ø × start chance)
    empty_slot_points: float = 100.0  # an empty slot in the eleven costs a whole player
    max_players: int = 4  # largest combination that is searched


@dataclass(frozen=True)
class SalePlayer:
    name: str
    value: int  # market value
    average_points: int = 0  # Kickbase season average per appearance
    start_percent: int | None = None  # start chance next matchday, None: unknown
    position: m.Position | None = None
    out: bool = False  # injured/suspended: doesn't count for the eleven anyway
    id: str | None = None

    def season_loss(self, w: GuideWeights) -> float:
        return w.season * self.average_points

    def matchday_loss(self, w: GuideWeights) -> float:
        start = 0.5 if self.start_percent is None else self.start_percent / 100
        return w.matchday * self.average_points * start

    def loss(self, w: GuideWeights) -> float:
        """Points the team gives up by selling this player."""
        return self.season_loss(w) + self.matchday_loss(w)


@dataclass(frozen=True)
class SellOption:
    players: tuple[SalePlayer, ...]  # highest value first
    total: int
    covers: bool  # True when selling these reaches zero
    surplus: int = 0  # account after selling (total − deficit), may be negative
    lost_average: int = 0  # sum of the sold players' season averages
    empty_after: int = 0  # slots that would stay empty in the eleven afterwards
    newly_empty: int = 0  # of those, slots this sale empties (others were empty before)
    # The cost the recommendation minimises, split up so every option can be compared:
    season_loss: float = 0.0  # season_weight · Σ Ø
    matchday_loss: float = 0.0  # matchday_weight · Σ Ø × start chance
    slot_loss: float = 0.0  # empty_slot_points · newly empty slots

    @property
    def points_lost(self) -> int:
        return round(self.season_loss + self.matchday_loss + self.slot_loss)


@dataclass(frozen=True)
class SellGuide:
    budget: int  # negative
    deficit: int  # positive amount that must be sold to reach zero
    weights: GuideWeights
    best: SellOption | None  # least points lost (None: no combination covers the deficit)
    fewest: SellOption  # fewest players: the most valuable first
    candidates: SellOption  # only the sell candidates (may not cover the deficit)


def _empty_slots(players: list[SalePlayer]) -> int:
    """Empty slots of the best formation with these players (injured ones don't count).
    Players without a position (old callers) fill any slot."""
    counts = {pos: 0 for pos in POSITIONS}
    free = 0
    for p in players:
        if p.out:
            continue
        if p.position is None:
            free += 1
        else:
            counts[p.position] += 1
    best = None
    for formation in (Formation(*f) for f in FORMATIONS):
        missing = sum(max(0, formation.slots(pos) - counts[pos]) for pos in POSITIONS)
        best = missing if best is None else min(best, missing)
    return max(0, (best or 0) - free)


def _option(
    picked: list[SalePlayer], squad: list[SalePlayer], deficit: int, w: GuideWeights
) -> SellOption:
    picked = sorted(picked, key=lambda p: -p.value)
    total = sum(p.value for p in picked)
    remaining = [p for p in squad if p not in picked]
    empty_after = _empty_slots(remaining)
    newly_empty = max(0, empty_after - _empty_slots(squad))
    return SellOption(
        players=tuple(picked),
        total=total,
        covers=total >= deficit,
        surplus=total - deficit,
        lost_average=sum(p.average_points for p in picked),
        empty_after=empty_after,
        newly_empty=newly_empty,
        season_loss=sum(p.season_loss(w) for p in picked),
        matchday_loss=sum(p.matchday_loss(w) for p in picked),
        slot_loss=newly_empty * w.empty_slot_points,
    )


def _pick(players: list[SalePlayer], deficit: int) -> list[SalePlayer]:
    picked, total = [], 0
    for p in players:
        picked.append(p)
        total += p.value
        if total >= deficit:
            break
    return picked


def _least_loss(
    squad: list[SalePlayer], deficit: int, w: GuideWeights, eligible: list[SalePlayer] | None = None
) -> list[SalePlayer] | None:
    """The cheapest combination (in points) that covers the deficit; ties: fewer players, then
    the smaller surplus. Exhaustive over up to `max_players` – a squad has at most ~17 players."""
    before = _empty_slots(squad)
    best_key, best = None, None
    available = squad if eligible is None else eligible
    for size in range(1, min(w.max_players, len(available)) + 1):
        for combo in combinations(available, size):
            total = sum(p.value for p in combo)
            if total < deficit:
                continue
            remaining = [p for p in squad if p not in combo]
            new_empty = max(0, _empty_slots(remaining) - before)
            cost = sum(p.loss(w) for p in combo) + new_empty * w.empty_slot_points
            key = (round(cost, 6), size, total)
            if best_key is None or key < best_key:
                best_key, best = key, list(combo)
    return best


def build_sell_guide(
    budget: int,
    squad: list[SalePlayer] | list[tuple[str, int]],
    candidates: list[SalePlayer] | list[tuple[str, int]],
    weights: GuideWeights | None = None,
    *,
    protected_ids: set[str] | None = None,
) -> SellGuide | None:
    """The guide, or None when the budget is not negative. Players may be given as plain
    (name, market value) tuples; then only the value-based options are meaningful."""
    deficit = -budget
    if deficit <= 0:
        return None
    w = weights or GuideWeights()
    squad_players = [p if isinstance(p, SalePlayer) else SalePlayer(*p) for p in squad]
    flagged_names = {(p.name if isinstance(p, SalePlayer) else p[0]) for p in candidates}
    flagged = [p for p in squad_players if p.name in flagged_names]
    flagged += [
        p if isinstance(p, SalePlayer) else SalePlayer(*p)
        for p in candidates
        if (p.name if isinstance(p, SalePlayer) else p[0]) not in {q.name for q in flagged}
    ]
    protected = protected_ids or set()
    eligible = [p for p in squad_players if p.id not in protected]
    flagged = [p for p in flagged if p.id not in protected]
    by_value = sorted(eligible, key=lambda p: -p.value)
    least = _least_loss(squad_players, deficit, w, eligible)
    return SellGuide(
        budget=budget,
        deficit=deficit,
        weights=w,
        best=_option(least, squad_players, deficit, w) if least is not None else None,
        fewest=_option(_pick(by_value, deficit), squad_players, deficit, w),
        candidates=_option(
            _pick(sorted(flagged, key=lambda p: -p.value), deficit), squad_players, deficit, w
        ),
    )
