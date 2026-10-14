"""Shared hold/sell rules: current movement and the real deadline, not a week-old trend."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.momentum import change_percent
from app.services.players import StartProbability
from app.services.scoring import TradingConfig


@dataclass(frozen=True)
class Holding:
    sell: bool
    verdict: str
    reasons: tuple[str, ...]
    trading: bool = False


def holding_signal(
    *,
    current: int,
    change_24h: int | None,
    start: StartProbability,
    deadline: datetime | None,
    now: datetime,
    config: TradingConfig,
) -> Holding:
    percent = change_percent(current, change_24h)
    window = deadline - now if deadline is not None else None
    before_weekend = window is not None and window > timedelta(hours=config.close_hours)
    risky = start.percent is not None and start.percent < 50
    if change_24h is not None and change_24h > 0 and before_weekend:
        return Holding(
            False,
            "Trading halten" if risky else "Halten – steigt aktuell",
            ("Marktwert steigt beim letzten Update; vor der Deadline erneut prüfen.",)
            + (
                ("Für die Spieltags-Elf unsicher; der Anstieg ist keine Gewinngarantie.",)
                if risky
                else ()
            ),
            trading=risky,
        )
    reasons = []
    if percent is not None and percent < config.daily_fall_percent:
        reasons.append(
            f"Marktwert {percent:+.1f} % in 24 Stunden".replace(".", ",").replace("-", "−")
        )
    if risky:
        reasons.append(f"Startchance {start.percent} %: {', '.join(start.reasons)}")
    if reasons:
        return Holding(True, "Verkaufskandidat", tuple(reasons))
    if change_24h is None:
        return Holding(
            False,
            "Halten – Tagestrend unbekannt",
            ("24-Stunden-Änderung fehlt; ein 7-Tage-Trend allein ist kein Verkaufssignal.",),
        )
    return Holding(False, "Halten", ())
