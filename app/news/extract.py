"""Extracts absences and returns from news texts with the language model.

The model only sees public Bundesliga news (title, teaser, tags) and may only pick a club from
our list. Its answer is checked against the schema, and a report is dropped when it points at an
unknown item or names a player whose surname doesn't occur in that item.
"""

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.client import LlmClient
from app.news.kicker import FeedItem
from app.news.matching import normalize

logger = logging.getLogger(__name__)

BATCH_SIZE = 12
OTHER_CLUB = "anderer Verein"


class Kind(StrEnum):
    INJURY = "verletzung"
    ILLNESS = "krankheit"
    SUSPENSION = "sperre"
    RETURN = "rueckkehr"
    OTHER = "sonstiges"


class ReportStatus(StrEnum):
    OUT = "faellt_aus"
    DOUBTFUL = "fraglich"
    REHAB = "aufbautraining"
    AVAILABLE = "einsatzbereit"


KIND_LABELS = {
    Kind.INJURY: "Verletzung",
    Kind.ILLNESS: "Krankheit",
    Kind.SUSPENSION: "Sperre",
    Kind.RETURN: "Rückkehr",
    Kind.OTHER: "Sonstiges",
}
STATUS_LABELS = {
    ReportStatus.OUT: "fällt aus",
    ReportStatus.DOUBTFUL: "fraglich",
    ReportStatus.REHAB: "im Aufbautraining",
    ReportStatus.AVAILABLE: "wieder einsatzbereit",
}


class ExtractedReport(BaseModel):
    nr: int = Field(description="Nummer der Meldung, aus der die Angabe stammt")
    spieler: str = Field(description="Name des Spielers wie im Text")
    verein: str = Field(description="Verein des Spielers aus der Vereinsliste")
    art: Kind
    status: ReportStatus
    ausfall: str | None = Field(description="Ausfalldauer laut Text, z. B. 'ca. drei Wochen'")
    rueckkehr: str | None = Field(description="Voraussichtliche Rückkehr laut Text")


class Extraction(BaseModel):
    meldungen: list[ExtractedReport]


def _output_model(clubs: Sequence[str]) -> type[Extraction]:
    """Same schema, but the club is an enum of our club names (plus 'anderer Verein'), so the
    model maps 'Werder-Profi' to 'Bremen' itself."""
    club_type = Literal[tuple([*clubs, OTHER_CLUB])]  # type: ignore[valid-type]

    class Report(ExtractedReport):
        verein: club_type  # type: ignore[valid-type]

    class ClubExtraction(Extraction):
        meldungen: list[Report]  # type: ignore[assignment]

    return ClubExtraction


SYSTEM = """Du wertest kurze Fußball-Nachrichten zur Bundesliga aus.
Gib für jeden Spieler, über dessen Verletzung, Krankheit, Sperre oder Rückkehr berichtet wird,
eine Angabe zurück. Regeln:
- Nur was ausdrücklich im Text steht; nichts vermuten oder ergänzen.
- Trainer, Funktionäre, Transfers, Vertragsthemen, Spielberichte sowie Fragen zu Stammplatz,
  Form oder Einsatzchancen ohne Verletzung/Krankheit/Sperre ignorieren.
- status: faellt_aus (verletzt/gesperrt/fehlt), fraglich (Einsatz wegen Blessur offen),
  aufbautraining (trainiert wieder, aber Spielfähigkeit nicht ausdrücklich genannt),
  einsatzbereit (nur wenn der Text Spielfähigkeit, Kader-Rückkehr oder einen Einsatz nennt).
- verein: genau ein Name aus der Vereinsliste; passt keiner, "anderer Verein".
- ausfall und rueckkehr nur, wenn der Text sie nennt, sonst null.
- Keine Angabe für Meldungen ohne solche Informationen."""


def build_prompt(items: Sequence[FeedItem], clubs: Sequence[str]) -> str:
    lines = ["Vereinsliste: " + ", ".join(clubs), "", "Meldungen:"]
    for nr, item in enumerate(items, start=1):
        tags = ", ".join(item.categories)
        lines.append(f"[{nr}] {item.title}\n{item.teaser}\nSchlagwörter: {tags}")
    return "\n\n".join(lines)


def _mentioned(report: ExtractedReport, item: FeedItem) -> bool:
    surname = normalize(report.spieler)[-1:]
    words = set(normalize(" ".join([item.title, item.teaser, *item.categories])))
    return bool(surname) and surname[0] in words


async def extract_batch(
    llm: LlmClient, batch: Sequence[FeedItem], clubs: Sequence[str]
) -> tuple[list[tuple[FeedItem, ExtractedReport]], int]:
    """One request for up to BATCH_SIZE items: the accepted reports and how many were dropped.
    Raises LlmError."""
    if len(batch) > BATCH_SIZE:
        raise ValueError(f"höchstens {BATCH_SIZE} Meldungen pro Aufruf")
    completion = await llm.complete(
        "news", SYSTEM, build_prompt(batch, clubs), "ausfaelle", _output_model(clubs)
    )
    results, dropped = [], 0
    for report in completion.data.meldungen:
        if not 1 <= report.nr <= len(batch) or not _mentioned(report, batch[report.nr - 1]):
            logger.warning(
                "KI-Angabe verworfen (Meldung %s, Spieler '%s' nicht im Text)",
                report.nr,
                report.spieler,
            )
            dropped += 1
            continue
        results.append((batch[report.nr - 1], report))
    return results, dropped
