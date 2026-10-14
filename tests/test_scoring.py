import pytest
from pydantic import ValidationError

from app.kickbase.models import Availability
from app.services.klasse import ClassInput
from app.services.scoring import (
    FormInput,
    ProgrammeInput,
    ScoreInputs,
    ScoringConfig,
    compute_score,
    load_scoring_config,
    trend_value,
)

CONFIG = load_scoring_config()
KLASSE = ClassInput(
    value=0.7,
    market_value=12_000_000,
    mv_percentile=0.7,
    average=None,
    avg_percentile=None,
    appearances=0,
    season_share=0.0,
)


def inputs(**overrides):
    values = {
        "availability": Availability.FIT,
        "status_label": "fit",
        "form": FormInput(percentile=0.8, weighted_points=120, matchdays=5, appearances=5),
        "trend_percent": 0.0,
        "programme": ProgrammeInput(0.6, ("Dortmund (A)", "Bremen (H)")),
        "einsatz": 0.6,
        "klasse": KLASSE,
    }
    return ScoreInputs(**(values | overrides))


def test_config_file_is_valid_and_complete():
    assert set(CONFIG.profiles) == {"kaufen", "verkaufen", "aufstellung"}
    for profile in CONFIG.profiles.values():
        w = profile.weights
        assert abs(w.form + w.trend + w.programm + w.einsatz + w.klasse - 1) < 0.01


def test_config_rejects_weights_not_summing_to_one():
    raw = CONFIG.model_dump()
    raw["profiles"]["kaufen"]["weights"]["form"] = 0.9
    with pytest.raises(ValidationError, match="muss 1 ergeben"):
        ScoringConfig.model_validate(raw)


def test_config_requires_threshold_min_score_and_seven_day_trend():
    for path, value in (
        (("profiles", "verkaufen", "trend_threshold_percent"), None),
        (("profiles", "kaufen", "min_score"), None),
        (("trend", "days"), 14),
    ):
        raw = CONFIG.model_dump()
        target = raw
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            ScoringConfig.model_validate(raw)


def test_config_rejects_unknown_keys_and_missing_profiles():
    raw = CONFIG.model_dump()
    raw["status_malus"]["verletzt"] = 1.0
    with pytest.raises(ValidationError):
        ScoringConfig.model_validate(raw)
    raw = CONFIG.model_dump()
    del raw["profiles"]["verkaufen"]
    with pytest.raises(ValidationError, match="Profile fehlen"):
        ScoringConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(None, 0.5), (0, 0.5), (15, 1.0), (30, 1.0), (-15, 0.0), (-7.5, 0.25)],
)
def test_trend_value(percent, expected):
    assert trend_value(percent, 15) == pytest.approx(expected)


def test_components_add_up_to_the_score():
    score = compute_score(inputs(), CONFIG.profiles["kaufen"], CONFIG)
    assert [c.key for c in score.components] == [
        "form",
        "trend",
        "programm",
        "einsatz",
        "klasse",
        "status",
    ]
    assert sum(c.points for c in score.components) == pytest.approx(score.total)
    # 0.25·0.8 + 0.25·0.5 + 0.15·0.6 + 0.15·0.6 + 0.20·0.7 = 0.645
    assert score.total == pytest.approx(64.5)
    assert not score.clamped


def test_status_malus_and_clamping():
    out = inputs(availability=Availability.OUT, status_label="verletzt")
    score = compute_score(out, CONFIG.profiles["kaufen"], CONFIG)
    assert score.component("status").points == pytest.approx(-100)
    assert score.total == 0 and score.clamped
    assert score.component("status").reason == "verletzt"


def test_missing_data_is_neutral_and_flagged():
    score = compute_score(
        inputs(form=None, trend_percent=None, programme=None, einsatz=None, klasse=None),
        CONFIG.profiles["kaufen"],
        CONFIG,
    )
    keys = ("form", "trend", "programm", "einsatz", "klasse")
    assert [score.component(k).value for k in keys] == [0.5] * 5
    assert score.missing == ("Form", "Trend", "Programm", "Einsatz", "Klasse")
    assert "neutral angesetzt" in score.component("form").reason
    assert score.component("trend").reason == "kein Marktwertverlauf – neutral angesetzt"
    assert score.component("programm").reason == "kein Programm verfügbar – neutral angesetzt"
    assert "neutral angesetzt" in score.component("einsatz").reason
    assert not compute_score(inputs(), CONFIG.profiles["kaufen"], CONFIG).incomplete


def test_player_without_appearances_is_not_missing_data():
    # ranked 29 % among the many players with 0 points - but no appearances means no form
    no_games = FormInput(percentile=0.29, weighted_points=0, matchdays=5, appearances=0)
    score = compute_score(inputs(form=no_games), CONFIG.profiles["kaufen"], CONFIG)
    assert score.component("form").value == 0 and not score.incomplete
    assert score.component("form").reason == "keine Einsätze in den letzten 5 Spieltagen"


def test_reasons_are_readable():
    score = compute_score(inputs(trend_percent=-12.63), CONFIG.profiles["verkaufen"], CONFIG)
    assert score.component("trend").reason == "Marktwert −12,6 % in 7 Tagen"
    assert score.component("form").reason == (
        "Ø 120 Punkte (gewichtet, 5 von 5 Spieltagen gespielt), besser als 80 % der Position"
    )
    assert score.component("programm").reason == "nächste Gegner: Dortmund (A), Bremen (H)"


def test_lineup_profile_ignores_trend():
    rising = compute_score(inputs(trend_percent=15), CONFIG.profiles["aufstellung"], CONFIG)
    falling = compute_score(inputs(trend_percent=-15), CONFIG.profiles["aufstellung"], CONFIG)
    assert rising.total == falling.total


def test_broken_config_stops_the_app(tmp_path, settings, monkeypatch):
    from app import main
    from app.services import scoring

    broken = tmp_path / "scoring.yaml"
    broken.write_text("profiles: {}\n")

    def load_broken():
        return scoring.load_scoring_config.__wrapped__(broken)

    monkeypatch.setattr(main, "load_scoring_config", load_broken)
    with pytest.raises(ValidationError):
        main.create_app(settings)


@pytest.mark.parametrize(
    "parts",
    [
        (17.5, 25.0, 0.0, 0.0),  # 42.5: rounds to 42 or 43, parts must agree
        (18.4, 22.4, 0.4, 0.0),  # three parts losing to rounding
        (32.6, 10.6, 15.6, -30.0),
        (12.5, 12.5, 12.5, 0.0),
    ],
)
def test_shown_parts_add_up_to_shown_total(parts):
    from app.services.scoring import Component, Score

    keys = ("form", "trend", "programm", "status")
    components = tuple(Component(k, k, 0, 0, p, "") for k, p in zip(keys, parts, strict=True))
    score = Score(total=sum(parts), components=components, clamped=False)
    assert sum(score.shown_points.values()) == score.shown_total
    for key, part in zip(keys, parts, strict=True):
        assert abs(score.shown_points[key] - part) < 1


def test_shown_parts_of_real_scores_add_up():
    for availability in Availability:
        for trend in (-20, -3.3, 0, 4.4, 40):
            for profile in CONFIG.profiles.values():
                score = compute_score(
                    inputs(availability=availability, trend_percent=trend), profile, CONFIG
                )
                if not score.clamped:
                    assert sum(score.shown_points.values()) == score.shown_total


def test_klasse_reason_explains_both_parts():
    k = ClassInput(0.9, 30_850_000, 0.97, 173.0, 0.98, 4, 0.25)
    score = compute_score(inputs(klasse=k), CONFIG.profiles["verkaufen"], CONFIG)
    reason = score.component("klasse").reason
    assert reason.startswith("Marktwert 30,85")
    assert "besser als 97 % der Position" in reason
    assert "Saison-Ø 173 in 4 Spielen – besser als 98 % (zählt 25 %)" in reason


def test_a_top_player_with_a_knock_is_no_longer_below_average():
    # Like Maza on 09.10.2026: top class and form, "angeschlagen", start chance 50 %.
    star = inputs(
        availability=Availability.DOUBTFUL,
        status_label="angeschlagen",
        form=FormInput(percentile=0.95, weighted_points=170, matchdays=4, appearances=4),
        trend_percent=0.8,
        einsatz=0.5,
        klasse=ClassInput(0.97, 30_850_000, 0.97, 173.0, 0.98, 4, 0.25),
    )
    assert compute_score(star, CONFIG.profiles["verkaufen"], CONFIG).total > 55
