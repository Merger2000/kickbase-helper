"""Explainable player scores.

Score = 100 · clamp(form·w_form + trend·w_trend + programme·w_programme + einsatz·w_einsatz
                    − malus·w_status, 0, 1)

Form, trend, programme and einsatz are normalised to 0…1, the status malus as well. Every score
keeps its components (raw value, weight, contribution in score points and a human-readable
reason), so the UI can show exactly how it came about. Weights live in `config/scoring.yaml`.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kickbase.models import Availability

if TYPE_CHECKING:
    from app.services.klasse import ClassInput

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Weights(StrictModel):
    form: float = Field(ge=0, le=1)
    trend: float = Field(ge=0, le=1)
    programm: float = Field(ge=0, le=1)
    einsatz: float = Field(ge=0, le=1)
    klasse: float = Field(default=0.0, ge=0, le=1)
    status: float = Field(ge=0, le=2)

    @model_validator(mode="after")
    def _positive_parts_sum_to_one(self) -> "Weights":
        total = self.form + self.trend + self.programm + self.einsatz + self.klasse
        if abs(total - 1) > 0.01:
            raise ValueError(
                f"form + trend + programm + einsatz + klasse muss 1 ergeben, ist {total:.2f}"
            )
        return self


class Profile(StrictModel):
    weights: Weights
    min_score: float | None = Field(default=None, ge=0, le=100)
    trend_threshold_percent: float | None = None


class FormConfig(StrictModel):
    recency_weights: list[float] = Field(min_length=1, max_length=10)


class TrendConfig(StrictModel):
    # Kickbase's own change for squad players (`sdmvt`) always covers 7 days.
    days: int = Field(ge=7, le=7)
    cap_percent: float = Field(gt=0, le=100)


class ProgrammeConfig(StrictModel):
    matches: int = Field(ge=1, le=6)
    home_factor: float = Field(gt=0, le=2)
    away_factor: float = Field(gt=0, le=2)
    blend_previous_season_until: int = Field(ge=0, le=34)
    promoted_points_per_game: float = Field(ge=0, le=3)


class StatusMalus(StrictModel):
    fit: float = Field(ge=0, le=1)
    fraglich: float = Field(ge=0, le=1)
    aufbautraining: float = Field(ge=0, le=1)
    faellt_aus: float = Field(ge=0, le=1)
    unbekannt: float = Field(ge=0, le=1)

    def for_availability(self, availability: Availability) -> float:
        return {
            Availability.FIT: self.fit,
            Availability.DOUBTFUL: self.fraglich,
            Availability.REHAB: self.aufbautraining,
            Availability.OUT: self.faellt_aus,
            Availability.UNKNOWN: self.unbekannt,
        }[availability]


class BidConfig(StrictModel):
    quantile: float = Field(gt=0, lt=1)
    period_days: int = Field(ge=7, le=365)
    price_bounds: list[int] = Field(min_length=1)
    min_comparable: int = Field(ge=3)

    @model_validator(mode="after")
    def _ascending_bounds(self) -> "BidConfig":
        if self.price_bounds != sorted(set(self.price_bounds)):
            raise ValueError("price_bounds müssen aufsteigend und eindeutig sein")
        return self


class KlasseConfig(StrictModel):
    # The season average counts at most this much in "Klasse" (the rest: market value rank) …
    max_season_share: float = Field(default=0.5, ge=0, le=1)
    # … and reaches it after this many appearances.
    full_after_appearances: int = Field(default=8, ge=1)


class SellGuideConfig(StrictModel):
    season_weight: float = Field(default=0.6, ge=0)
    matchday_weight: float = Field(default=0.4, ge=0)
    empty_slot_points: float = Field(default=100.0, ge=0)
    max_players: int = Field(default=4, ge=1, le=4)  # exhaustive search: keep it small


class XpConfig(StrictModel):
    # Wert (0…1) für fehlende Daten in einem der Faktoren. 0,5 ist neutral.
    neutral: float = Field(default=0.5, gt=0, le=1)


class TradingConfig(StrictModel):
    close_hours: int = Field(default=24, ge=2, le=72)
    safety_hours: int = Field(default=2, ge=1, le=12)
    min_daily_gain: int = Field(default=20_000, ge=0)
    daily_fall_percent: float = Field(default=-0.25, ge=-20, le=0)


class ScoringConfig(StrictModel):
    form: FormConfig
    trend: TrendConfig
    programme: ProgrammeConfig
    status_malus: StatusMalus
    profiles: dict[str, Profile]
    bids: BidConfig
    sell_guide: SellGuideConfig = SellGuideConfig()
    klasse: KlasseConfig = KlasseConfig()
    xp: XpConfig = XpConfig()
    trading: TradingConfig = TradingConfig()

    @model_validator(mode="after")
    def _known_profiles(self) -> "ScoringConfig":
        missing = {"kaufen", "verkaufen", "aufstellung"} - set(self.profiles)
        if missing:
            raise ValueError(f"Profile fehlen: {', '.join(sorted(missing))}")
        if self.profiles["verkaufen"].trend_threshold_percent is None:
            raise ValueError("verkaufen braucht trend_threshold_percent")
        if self.profiles["kaufen"].min_score is None:
            raise ValueError("kaufen braucht min_score")
        return self


@lru_cache
def load_scoring_config(path: Path = CONFIG_DIR / "scoring.yaml") -> ScoringConfig:
    return ScoringConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class FormInput:
    percentile: float  # 0…1 within the position
    weighted_points: float
    matchdays: int  # how many matchdays went into it
    appearances: int = 0  # of those, in how many the player was used


@dataclass(frozen=True)
class ProgrammeInput:
    value: float  # 1 − mean difficulty, 0…1
    fixtures: tuple[str, ...]  # e.g. ("Dortmund (A)", "Bremen (H)")


@dataclass(frozen=True)
class ScoreInputs:
    availability: Availability
    status_label: str
    form: FormInput | None
    trend_percent: float | None
    programme: ProgrammeInput | None
    einsatz: float | None = None  # start probability 0…1, None without data
    klasse: "ClassInput | None" = None  # market value rank + season average, None: unknown


# ---------------------------------------------------------------------------- result


@dataclass(frozen=True)
class Component:
    key: str  # form / trend / programm / einsatz / status
    label: str
    value: float  # normalised 0…1 (for status: the malus)
    weight: float
    points: float  # contribution in score points (negative for the status malus)
    reason: str


@dataclass(frozen=True)
class Score:
    total: float  # 0…100
    components: tuple[Component, ...]
    clamped: bool  # the raw sum was outside 0…100
    # Components that had no data and were set to neutral (shown as a hint in the UI).
    missing: tuple[str, ...] = ()

    @property
    def incomplete(self) -> bool:
        return bool(self.missing)

    @property
    def shown_total(self) -> int:
        return round(self.total)

    @property
    def shown_points(self) -> dict[str, int]:
        """Whole-number contributions that add up exactly to `shown_total` (largest remainder),
        so the explanation never disagrees with the score by a rounding point. Clamped scores
        keep the plain rounded parts; the UI says that the total was limited."""
        rounded = {c.key: round(c.points) for c in self.components}
        if self.clamped:
            return rounded
        diff = self.shown_total - sum(rounded.values())
        step = 1 if diff > 0 else -1
        # Adjust the parts that lost the most in rounding (in the needed direction) first.
        order = sorted(
            self.components, key=lambda c: (c.points - rounded[c.key]) * step, reverse=True
        )
        for component in order[: abs(diff)]:
            rounded[component.key] += step
        return rounded

    def component(self, key: str) -> Component:
        return next(c for c in self.components if c.key == key)


NEUTRAL = 0.5


def trend_value(percent: float | None, cap: float) -> float:
    if percent is None:
        return NEUTRAL
    return NEUTRAL + max(-cap, min(cap, percent)) / (2 * cap)


def compute_score(inputs: ScoreInputs, profile: Profile, config: ScoringConfig) -> Score:
    w = profile.weights
    if inputs.form is None:
        form = NEUTRAL
    elif inputs.form.appearances == 0:
        form = 0.0  # not used at all: no form, whatever the rank among the other non-players
    else:
        form = inputs.form.percentile
    trend = trend_value(inputs.trend_percent, config.trend.cap_percent)
    programme = inputs.programme.value if inputs.programme else NEUTRAL
    einsatz = inputs.einsatz if inputs.einsatz is not None else NEUTRAL
    klasse = inputs.klasse.value if inputs.klasse is not None else NEUTRAL
    malus = config.status_malus.for_availability(inputs.availability)

    components = (
        Component("form", "Form", form, w.form, 100 * w.form * form, _form_reason(inputs.form)),
        Component(
            "trend",
            "Trend",
            trend,
            w.trend,
            100 * w.trend * trend,
            _trend_reason(inputs.trend_percent, config.trend.days),
        ),
        Component(
            "programm",
            "Programm",
            programme,
            w.programm,
            100 * w.programm * programme,
            _programme_reason(inputs.programme),
        ),
        Component(
            "einsatz",
            "Einsatz",
            einsatz,
            w.einsatz,
            100 * w.einsatz * einsatz,
            _einsatz_reason(inputs.einsatz),
        ),
        Component(
            "klasse",
            "Klasse",
            klasse,
            w.klasse,
            100 * w.klasse * klasse,
            inputs.klasse.reason if inputs.klasse else "keine Daten – neutral angesetzt",
        ),
        Component(
            "status", "Status", malus, w.status, -100 * w.status * malus, inputs.status_label
        ),
    )
    raw = sum(c.points for c in components)
    total = max(0.0, min(100.0, raw))
    missing = tuple(
        label
        for label, value in (
            ("Form", inputs.form),
            ("Trend", inputs.trend_percent),
            ("Programm", inputs.programme),
            ("Einsatz", inputs.einsatz),
            ("Klasse", inputs.klasse if w.klasse else 0),
        )
        if value is None
    )
    return Score(total=total, components=components, clamped=total != raw, missing=missing)


def _form_reason(form: FormInput | None) -> str:
    if form is None:
        return "keine Formdaten (Spieler noch nicht erfasst) – neutral angesetzt"
    if form.appearances == 0:
        return f"keine Einsätze in den letzten {form.matchdays} Spieltagen"
    better = round(form.percentile * 100)
    return (
        f"Ø {form.weighted_points:.0f} Punkte (gewichtet, {form.appearances} von "
        f"{form.matchdays} Spieltagen gespielt), besser als {better} % der Position"
    )


def _trend_reason(percent: float | None, days: int) -> str:
    if percent is None:
        return "kein Marktwertverlauf – neutral angesetzt"
    sign = "+" if percent > 0 else ("−" if percent < 0 else "")
    amount = f"{abs(percent):.1f}".replace(".", ",")
    return f"Marktwert {sign}{amount} % in {days} Tagen"


def _programme_reason(programme: ProgrammeInput | None) -> str:
    if programme is None or not programme.fixtures:
        return "kein Programm verfügbar – neutral angesetzt"
    return "nächste Gegner: " + ", ".join(programme.fixtures)


def _einsatz_reason(einsatz: float | None) -> str:
    if einsatz is None:
        return "keine Einsatzdaten (Startwahrscheinlichkeit) – neutral angesetzt"
    return f"Startwahrscheinlichkeit {round(einsatz * 100)} %"
