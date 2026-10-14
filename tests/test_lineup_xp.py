"""C4.2: Beste Elf nach xP.

`choose_formation` maximiert die Summe der erwarteten Punkte (xP = Startchance × Form ×
Gegnerstärke). Die Bildung ist weiterhin auf erlaubte Formationen beschränkt und Positionen
werden respektiert – die Wahl erfolgt aber nach xP statt nach dem zusammenfassenden Score.
"""

import pytest

from app.kickbase.models import Position
from app.services.players import StartProbability
from app.services.recommendations import (
    Candidate,
    Recommendation,
    choose_formation,
)
from app.services.scoring import Score
from app.services.xp import XP, XPComponent


def _player(pid: str, position: Position) -> Candidate:
    return Candidate(pid, pid, "1", None, position, 0, None)


def _rec(
    pid: str, position: Position, *, xp_value: float, score_total: float = 50.0
) -> Recommendation:
    """Baut eine Recommendation mit vorgegebenem xP-Wert (Startchance × Form × Gegner)."""
    xp = XP(
        value=xp_value,
        startchance=xp_value,
        form=1.0,
        gegner=1.0,
        parts=(
            XPComponent("startchance", "Startchance", xp_value, 1.0, xp_value, "x"),
            XPComponent("form", "Form", 1.0, 1.0, 1.0, "x"),
            XPComponent("gegner", "Gegner", 1.0, 1.0, 1.0, "x"),
        ),
    )
    return Recommendation(
        player=_player(pid, position),
        score=Score(score_total, (), False),
        market_value=1,
        xp=xp,
        start=StartProbability(round(xp_value * 100), ("x",)),
    )


def test_choose_formation_picks_highest_total_xp_for_a_full_eleven():
    # Gleicher Score (50), aber deutlich unterschiedliches xP – die Elf muss nach xP gewinnen.
    squad = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.70 - i * 0.05) for i in range(4)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60 - i * 0.05) for i in range(4)]
        + [_rec(f"s{i}", Position.FORWARD, xp_value=0.50 - i * 0.05) for i in range(2)]
    )
    formation, picked = choose_formation(squad)
    assert formation.label == "4-4-2"
    chosen = [r for players in picked.values() for r in players]
    # 4-4-2 ist die einzige vollständige erlaubte Formation mit 11 Spielern.
    assert len(chosen) == 11
    # Die besten xP-Werte pro Position wurden gezogen.
    assert [r.player.id for r in picked[Position.DEFENDER]] == ["d0", "d1", "d2", "d3"]
    assert [r.player.id for r in picked[Position.MIDFIELDER]] == ["m0", "m1", "m2", "m3"]
    assert [r.player.id for r in picked[Position.FORWARD]] == ["s0", "s1"]
    # Summe der xP-Werte ist maximal unter allen erlaubten 4-4-2-Besetzungen.
    expected_total = (
        0.80
        + sum(0.70 - i * 0.05 for i in range(4))
        + sum(0.60 - i * 0.05 for i in range(4))
        + sum(0.50 - i * 0.05 for i in range(2))
    )
    assert sum(r.xp.value for r in chosen) == pytest.approx(expected_total)


def test_choose_formation_prefers_higher_xp_when_swapping_two_players():
    # Bei sonst gleicher Lage wählt die Funktion den Spieler mit dem höheren xP,
    # auch wenn sein Score niedriger ist. dB hat den höchsten Score aller Verteidiger,
    # aber das niedrigste xP – er fällt aus der Viererkette raus.
    squad = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.90)]
        + [
            _rec("dA", Position.DEFENDER, xp_value=0.80, score_total=30),
            _rec("dB", Position.DEFENDER, xp_value=0.20, score_total=99),
            _rec("dC", Position.DEFENDER, xp_value=0.70),
            _rec("dD", Position.DEFENDER, xp_value=0.60),
            _rec("dE", Position.DEFENDER, xp_value=0.55),
        ]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60) for i in range(4)]
        + [_rec("s0", Position.FORWARD, xp_value=0.55), _rec("s1", Position.FORWARD, xp_value=0.45)]
    )
    formation, picked = choose_formation(squad)
    chosen_defenders = picked[Position.DEFENDER]
    chosen_ids = [r.player.id for r in chosen_defenders]
    # 4-4-2 gewinnt: dA, dC, dD, dE – dB (Score 99, xP 0,20) ist draußen.
    assert formation.label == "4-4-2"
    assert "dA" in chosen_ids
    assert "dB" not in chosen_ids
    # dA (höchstes xP) steht in der Aufstellung ganz vorne.
    assert chosen_ids[0] == "dA"


def test_choose_formation_excludes_disallowed_formations():
    # 6 Verteidiger passen in keine erlaubte Formation; mit zwei Notbesetzungen im Sturm
    # wird trotzdem eine vollständige Elf gestellt.
    squad = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.50) for i in range(6)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60) for i in range(4)]
    )
    risky = [_rec(f"s{i}", Position.FORWARD, xp_value=0.10) for i in range(2)]
    formation, picked = choose_formation(squad, risky)
    from app.services.lineup import FORMATIONS

    # 6-4-0 ist nicht erlaubt; die Funktion wählt eine erlaubte Formation aus FORMATIONS.
    assert (formation.defenders, formation.midfielders, formation.forwards) in FORMATIONS
    chosen = [r for players in picked.values() for r in players]
    assert len(chosen) == 11
    # Kein Spieler aus dem Kader wurde doppelt gezogen.
    chosen_ids = [r.player.id for r in chosen]
    assert len(chosen_ids) == len(set(chosen_ids))


def test_choose_formation_skips_stand_ins_when_xp_is_zero():
    # Spieler mit xP = 0 (verletzt / nicht in der Startelf) darf auch als Notbesetzung
    # keinen leeren Platz füllen – die Formation bleibt offen.
    from dataclasses import replace

    fit = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.60) for i in range(4)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60) for i in range(5)]
    )
    risky = [
        replace(
            _rec("s-zero", Position.FORWARD, xp_value=0.0),
            start=StartProbability(0, ("x",)),
        )
    ]
    _, picked = choose_formation(fit, risky)
    # 4-5-1 hat einen Stürmerplatz; ohne fit-Stürmer bleibt er leer, weil die
    # Notbesetzung xP = 0 hat (kein sinnvoller Beitrag).
    assert sum(len(p) for p in picked.values()) == 10
    assert picked[Position.FORWARD] == []


def test_choose_formation_fills_empty_slot_with_highest_xp_stand_in():
    # Wenn Notbesetzungen unterschiedliches xP haben, wird die mit dem höchsten xP genommen.
    from dataclasses import replace

    fit = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.60) for i in range(4)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60) for i in range(5)]
    )
    unlikely = [
        replace(
            _rec("s-low", Position.FORWARD, xp_value=0.05),
            start=StartProbability(15, ("x",)),
        ),
        replace(
            _rec("s-high", Position.FORWARD, xp_value=0.20),
            start=StartProbability(40, ("y",)),
        ),
    ]
    formation, picked = choose_formation(fit, unlikely)
    # 4-5-1 hat genau einen Stürmerplatz; s-high (xP 0,20) wird genommen.
    assert formation.label == "4-5-1"
    assert [r.player.id for r in picked[Position.FORWARD]] == ["s-high"]


def test_choose_formation_is_xp_driven_even_when_score_differs_greatly():
    # Stress-Test: ein fit Spieler mit niedrigem xP verliert gegen Spieler mit hohem xP,
    # obwohl sein Score viel besser ist. Das war der zentrale Unterschied zu C3.
    fit = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [
            _rec("d-low-xp", Position.DEFENDER, xp_value=0.20, score_total=99),
            _rec("d-high-xp", Position.DEFENDER, xp_value=0.90, score_total=10),
        ]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.60) for i in range(3)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.60) for i in range(4)]
        + [_rec(f"s{i}", Position.FORWARD, xp_value=0.55) for i in range(2)]
    )
    _, picked = choose_formation(fit)
    # 4-4-2 nimmt die vier besten Verteidiger nach xP – d-low-xp (Score 99, xP 0,20) ist draußen.
    chosen_defenders = picked[Position.DEFENDER]
    chosen_ids = [r.player.id for r in chosen_defenders]
    assert "d-high-xp" in chosen_ids
    assert "d-low-xp" not in chosen_ids
    assert sum(r.xp.value for r in chosen_defenders) == pytest.approx(0.90 + 0.60 * 3)


def test_choose_formation_uses_score_fallback_when_xp_missing():
    # Wenn kein xP gesetzt ist, fällt die Funktion auf den Score zurück – bestehende
    # Aufrufer (Tests, andere Services) bleiben damit lauffähig.
    squad = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.80)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.50) for i in range(4)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.50) for i in range(4)]
        + [_rec(f"s{i}", Position.FORWARD, xp_value=0.50) for i in range(2)]
    )
    no_xp = [
        Recommendation(
            player=r.player,
            score=Score(80 - i, (), False),
            market_value=1,
            xp=None,
        )
        for i, r in enumerate(squad)
    ]
    formation, picked = choose_formation(no_xp)
    assert formation.label == "4-4-2"
    # Maximaler Score wurde zuerst genommen.
    assert picked[Position.DEFENDER][0].score.total == 80 - 1


def test_playable_stand_in_with_zero_form_index_still_fills_a_slot():
    from dataclasses import replace

    fit = (
        [_rec("tw", Position.GOALKEEPER, xp_value=0.8)]
        + [_rec(f"d{i}", Position.DEFENDER, xp_value=0.6) for i in range(4)]
        + [_rec(f"m{i}", Position.MIDFIELDER, xp_value=0.6) for i in range(5)]
    )
    fallback = replace(
        _rec("returning-forward", Position.FORWARD, xp_value=0),
        start=StartProbability(15, ("Rückkehr nach Pause",)),
    )
    _, picked = choose_formation(fit, [fallback])
    assert sum(len(players) for players in picked.values()) == 11
    assert picked[Position.FORWARD] == [fallback]
