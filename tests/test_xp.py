import pytest
from pydantic import ValidationError

from app.services.players import StartProbability
from app.services.scoring import (
    FormInput,
    ProgrammeInput,
    XpConfig,
    load_scoring_config,
)
from app.services.xp import expected_points

CONFIG = load_scoring_config().xp


def form(percentile: float = 0.7, appearances: int = 5, matchdays: int = 5) -> FormInput:
    return FormInput(
        percentile=percentile,
        weighted_points=120.0,
        matchdays=matchdays,
        appearances=appearances,
    )


def programme(value: float = 0.6) -> ProgrammeInput:
    return ProgrammeInput(value, ("Dortmund (A)", "Bremen (H)"))


def start(percent: int | None = 85) -> StartProbability:
    return StartProbability(percent, ("voraussichtliche Startelf (LigaInsider)",))


def test_config_neutral_must_be_between_zero_and_one():
    for bad in (0.0, 1.1, -0.1):
        with pytest.raises(ValidationError):
            XpConfig(neutral=bad)


def test_full_player_gets_product_of_three_factors():
    xp = expected_points(start=start(80), form=form(0.7), programme=programme(0.6), config=CONFIG)
    assert xp.value == pytest.approx(0.80 * 0.7 * 0.6)
    assert xp.startchance == pytest.approx(0.8)
    assert xp.form == pytest.approx(0.7)
    assert xp.gegner == pytest.approx(0.6)
    assert xp.missing == ()
    assert not xp.incomplete
    assert {p.key for p in xp.parts} == {"startchance", "form", "gegner"}
    assert all(p.weight == 1.0 for p in xp.parts)
    assert "Startchance 80 %" in xp.parts[0].reason
    assert "besser als 70 %" in xp.parts[1].reason
    assert "Dortmund" in xp.parts[2].reason


def test_form_factor_caps_top_percentile_at_99_to_avoid_better_than_100():
    # Perzentil 1,0 (bester der Position) darf nicht als „besser als 100 %" erscheinen –
    # das klingt nach Widerspruch und hilft niemandem beim Lesen.
    xp = expected_points(start=start(80), form=form(1.0), programme=programme(0.6), config=CONFIG)
    assert "besser als 99 %" in xp.parts[1].reason
    assert "100 %" not in xp.parts[1].reason
    # Der Faktor selbst bleibt 1,0 – die Anzeige wird nur gekappt.
    assert xp.form == 1.0


def test_injured_player_has_zero_xp_even_with_great_form_and_opponent():
    xp = expected_points(
        start=start(0),
        form=form(0.95),
        programme=programme(0.8),
        config=CONFIG,
    )
    assert xp.value == 0
    assert xp.startchance == 0
    assert not xp.incomplete  # 0 % ist eine Aussage, kein fehlender Wert
    assert "Startchance 0 %" in xp.parts[0].reason


def test_starting_player_outscores_bench_player_with_same_form_and_opponent():
    starter = expected_points(start=start(85), form=form(), programme=programme(), config=CONFIG)
    bench = expected_points(start=start(15), form=form(), programme=programme(), config=CONFIG)
    assert starter.value > bench.value
    assert pytest.approx(starter.value / bench.value, rel=1e-6) == 85 / 15


def test_stronger_opponent_reduces_xp_keeping_other_factors_constant():
    weak = expected_points(start=start(80), form=form(), programme=programme(0.8), config=CONFIG)
    strong = expected_points(start=start(80), form=form(), programme=programme(0.3), config=CONFIG)
    assert weak.value > strong.value
    assert pytest.approx(weak.value / strong.value, rel=1e-6) == 0.8 / 0.3


def test_missing_data_uses_neutral_and_flags_missing_factors():
    xp = expected_points(start=start(), form=None, programme=None, config=CONFIG)
    assert xp.value == pytest.approx(0.85 * CONFIG.neutral * CONFIG.neutral)
    assert xp.form == CONFIG.neutral
    assert xp.gegner == CONFIG.neutral
    assert xp.missing == ("Form", "Gegner")
    assert xp.incomplete
    assert "neutral angesetzt" in xp.parts[1].reason
    assert "neutral angesetzt" in xp.parts[2].reason


def test_start_without_percent_is_treated_as_missing_neutral():
    xp = expected_points(
        start=StartProbability(None, ("keine Prognose vorhanden",)),
        form=form(),
        programme=programme(),
        config=CONFIG,
    )
    assert xp.startchance == CONFIG.neutral
    assert "Startchance" in xp.missing
    assert "keine Prognose" in xp.parts[0].reason


def test_player_with_no_form_appearances_drops_to_zero_not_neutral():
    xp = expected_points(
        start=start(90),
        form=form(percentile=0.0, appearances=0),
        programme=programme(),
        config=CONFIG,
    )
    assert xp.value == 0  # 0,90 · 0 · … = 0
    assert xp.form == 0.0
    assert not xp.incomplete  # Perzentil 0 ist eine Aussage, nicht „fehlend"
    assert "keine Einsätze" in xp.parts[1].reason


def test_startchance_zero_dominates_even_when_form_and_opponent_are_perfect():
    xp = expected_points(
        start=start(0),
        form=form(1.0, appearances=5),
        programme=programme(1.0),
        config=CONFIG,
    )
    assert xp.value == 0
    # Die anderen Faktoren bleiben sichtbar, damit die UI 0 erklären kann.
    assert xp.form == 1.0
    assert xp.gegner == 1.0


def test_programme_without_fixtures_is_missing_not_zero():
    xp = expected_points(
        start=start(),
        form=form(),
        programme=ProgrammeInput(0.0, ()),
        config=CONFIG,
    )
    assert xp.gegner == CONFIG.neutral
    assert "Gegner" in xp.missing


def test_missing_start_uses_neutral_and_value_does_not_exceed_neutral_squared():
    xp = expected_points(start=None, form=form(), programme=programme(), config=CONFIG)
    assert xp.startchance == CONFIG.neutral
    assert "Startchance" in xp.missing
    # Ohne Startchance kann xP nicht über den neutralen Anteil hinaus kommen.
    assert xp.value < 0.7  # form=0.7, programme=0.6, start=neutral=0.5 → 0.21
