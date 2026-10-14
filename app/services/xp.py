"""Relative lineup index (legacy internal name xP), not a points prediction.

The product of start probability, positional form rank and multi-match programme is
dimensionless. Display it on a 0–100 scale without probability or Kickbase-points units.
Internal names remain compatible with C4; a calibrated points model needs a separate backtest.

xP = Startchance × Form × Gegnerstärke – alle Faktoren liegen zwischen 0 und 1. Verletzte oder
nicht im Kader stehende Spieler bekommen xP = 0, egal wie gut Form und Gegner wären: ohne
Einsatz keine Punkte. Fehlt einer der Faktoren, wird der Neutralwert aus ``config/scoring.yaml``
(``xp.neutral``) angesetzt und die Lücke in ``XP.missing`` gemeldet, damit die UI sie anzeigen
kann.

Die Faktoren stammen aus vorhandenen Services: ``app.services.players.compute_start_probability``
(Startchance), ``app.services.form.FormInput`` (Perzentil-Rang) und ``app.services.programme.
ProgrammeInput`` (1 − mittlere Gegnerstärke). Spätere Quoten (Block C6) setzen an derselben
Stelle einen weiteren 0…1-Faktor ein – die Schnittstelle bleibt unverändert.
"""

from dataclasses import dataclass

from app.services.players import StartProbability
from app.services.scoring import FormInput, ProgrammeInput, XpConfig


@dataclass(frozen=True)
class XPComponent:
    key: str  # startchance / form / gegner
    label: str
    value: float  # 0…1
    weight: float  # 1.0: xP ist ein Produkt, jeder Faktor zählt mit Gewicht 1.
    points: float  # Beitrag zum Produkt (bei Produkt = value selbst, dokumentiert die Formel)
    reason: str


@dataclass(frozen=True)
class XP:
    value: float  # Produkt der Faktoren, 0…1; 0 für verletzte/nicht im Kader.
    startchance: float  # 0…1
    form: float  # 0…1
    gegner: float  # 0…1
    parts: tuple[XPComponent, ...]
    # Welche Faktoren ohne Daten waren (Startchance, Form, Gegner).
    missing: tuple[str, ...] = ()

    @property
    def incomplete(self) -> bool:
        return bool(self.missing)


def expected_points(
    *,
    start: StartProbability | None,
    form: FormInput | None,
    programme: ProgrammeInput | None,
    config: XpConfig,
) -> XP:
    """xP für einen Spieler. Verletzte/0-%-Chance → 0; sonst Produkt der drei Faktoren."""
    start_value, start_missing, start_reason = _start_factor(start, config.neutral)
    form_value, form_missing, form_reason = _form_factor(form, config.neutral)
    gegner_value, gegner_missing, gegner_reason = _gegner_factor(programme, config.neutral)
    parts = (
        XPComponent(
            "startchance",
            "Startchance",
            start_value,
            1.0,
            start_value,
            start_reason,
        ),
        XPComponent(
            "form",
            "Form",
            form_value,
            1.0,
            form_value,
            form_reason,
        ),
        XPComponent(
            "gegner",
            "Gegner",
            gegner_value,
            1.0,
            gegner_value,
            gegner_reason,
        ),
    )
    # Ohne Einsatz keine Punkte – die anderen Faktoren werden trotzdem angezeigt, damit die
    # Erklärung in der UI nicht leer ist („warum 0?").
    value = 0.0 if start_value == 0 else start_value * form_value * gegner_value
    missing = tuple(
        label
        for label, flag in (
            ("Startchance", start_missing),
            ("Form", form_missing),
            ("Gegner", gegner_missing),
        )
        if flag
    )
    return XP(
        value=value,
        startchance=start_value,
        form=form_value,
        gegner=gegner_value,
        parts=parts,
        missing=missing,
    )


def _start_factor(start: StartProbability | None, neutral: float) -> tuple[float, bool, str]:
    if start is None:
        return neutral, True, "keine Startprognose – neutral angesetzt"
    if start.percent is None:
        return neutral, True, f"{', '.join(start.reasons)} – neutral angesetzt"
    factor = start.percent / 100
    return factor, False, f"Startchance {start.percent} % ({', '.join(start.reasons)})"


def _form_factor(form: FormInput | None, neutral: float) -> tuple[float, bool, str]:
    if form is None:
        return neutral, True, "keine Formdaten – neutral angesetzt"
    if form.matchdays == 0:
        return neutral, True, "noch keine Spieltage erfasst – neutral angesetzt"
    if form.appearances == 0:
        # Kein Rang unter lauter Null-Punkt-Spielern: kein Form-Signal, also 0 statt Perzentil.
        return 0.0, False, f"keine Einsätze in den letzten {form.matchdays} Spieltagen"
    # „besser als 100 %" liest sich wie ein Widerspruch (man kann nicht besser als
    # alle sein) – Perzentile werden daher bei 99 % gekappt, wie es auch die
    # Bundesliga-Tabellen tun.
    pct = min(round(form.percentile * 100), 99)
    return (
        form.percentile,
        False,
        f"besser als {pct} % der Position ({form.appearances} von "
        f"{form.matchdays} Spieltagen gespielt)",
    )


def _gegner_factor(programme: ProgrammeInput | None, neutral: float) -> tuple[float, bool, str]:
    if programme is None or not programme.fixtures:
        return neutral, True, "kein Programm verfügbar – neutral angesetzt"
    return (
        programme.value,
        False,
        "nächste Gegner: " + ", ".join(programme.fixtures),
    )
