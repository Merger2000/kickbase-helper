"""Difficulty of each club's next matches, from the OpenLigaDB schedule and table."""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from app.kickbase.client import Fetched
from app.openligadb.client import OpenLigaDbClient, OpenLigaDbError
from app.openligadb.models import OldbMatch, OldbTableRow
from app.services.scoring import CONFIG_DIR, ProgrammeConfig, ProgrammeInput

logger = logging.getLogger(__name__)

MAX_POINTS_PER_GAME = 3.0
PREVIOUS_SEASON_TTL = 7 * 24 * 3600
BUNDESLIGA_TEAMS = 18
LAST_MATCHDAY = 34
# Matches further away than this are not part of the "next" programme (e.g. after a break).
HORIZON = timedelta(weeks=6)
_reported: set[str] = set()  # mapping problems are logged once per process, not per page view


def _log_once(key: str, level: int, message: str, *args: object) -> None:
    if key not in _reported:
        _reported.add(key)
        logger.log(level, message, *args)


@lru_cache
def load_team_mapping(path: Path = CONFIG_DIR / "team_mapping.yaml") -> dict[str, int]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): int(v) for k, v in (data.get("teams") or {}).items()}


def _normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text)


def map_teams(
    kickbase_teams: dict[str, str], table: list[OldbTableRow], configured: dict[str, int]
) -> tuple[dict[str, int], dict[str, str]]:
    """Kickbase team id -> OpenLigaDB team id (configured ids first, then names), plus the
    teams that could not be mapped with the reason."""
    known = {row.team_id for row in table}
    mapping: dict[str, int] = {}
    unmapped: dict[str, str] = {}
    for team_id, name in kickbase_teams.items():
        if team_id in configured:
            if configured[team_id] in known:
                mapping[team_id] = configured[team_id]
                continue
            _log_once(
                f"stale:{team_id}",
                logging.WARNING,
                "config/team_mapping.yaml: OpenLigaDB-Id %s für %s ist nicht in der Tabelle",
                configured[team_id],
                name,
            )
        wanted = _normalize(name)
        matches = [
            row
            for row in table
            if wanted
            and (
                wanted == _normalize(row.short_name)
                or wanted in _normalize(row.name)
                or (row.short_name and _normalize(row.short_name) in wanted)
            )
        ]
        if len(matches) == 1:
            _log_once(
                f"name:{team_id}",
                logging.WARNING,
                "Verein %s (%s) per Name auf OpenLigaDB %s (%s) abgebildet; "
                "bitte in config/team_mapping.yaml eintragen",
                team_id,
                name,
                matches[0].team_id,
                matches[0].name,
            )
            mapping[team_id] = matches[0].team_id
        else:
            reason = "mehrdeutig" if matches else "nicht gefunden"
            _log_once(
                f"none:{team_id}",
                logging.ERROR,
                "Verein %s (%s) in OpenLigaDB %s",
                team_id,
                name,
                reason,
            )
            unmapped[team_id] = f"Zuordnung {reason}"
    return mapping, unmapped


@dataclass(frozen=True)
class Programme:
    value: float
    fixtures: tuple[str, ...]

    def as_input(self) -> ProgrammeInput:
        return ProgrammeInput(value=self.value, fixtures=self.fixtures)


@dataclass(frozen=True)
class Programmes:
    by_team: dict[str, Programme]  # Kickbase team id
    fetched_at: datetime
    stale: bool
    # Kickbase team id -> why there is no programme (shown on the page)
    missing: dict[str, str] = field(default_factory=dict)


def team_strengths(
    current: list[OldbTableRow], previous: list[OldbTableRow], config: ProgrammeConfig
) -> dict[int, float]:
    """Points per game blended with last season early on, scaled to 0…1."""
    previous_ppg = {row.team_id: row.points_per_game for row in previous}
    strengths = {}
    for row in current:
        last = previous_ppg.get(row.team_id) or config.promoted_points_per_game
        now = row.points_per_game if row.points_per_game is not None else last
        blend = config.blend_previous_season_until
        share = 1.0 if blend == 0 else min(row.matches / blend, 1.0)
        ppg = share * now + (1 - share) * last
        strengths[row.team_id] = min(ppg / MAX_POINTS_PER_GAME, 1.0)
    return strengths


def programme_for(
    oldb_team: int,
    matches: list[OldbMatch],
    strengths: dict[int, float],
    names: dict[int, str],
    config: ProgrammeConfig,
    now: datetime | None = None,
) -> Programme | None:
    upcoming = sorted(
        (
            m
            for m in matches
            if not m.finished
            and oldb_team in (m.team1.team_id, m.team2.team_id)
            and (now is None or m.kickoff <= now + HORIZON)
        ),
        key=lambda m: m.kickoff,
    )[: config.matches]
    if not upcoming:
        return None
    difficulties, fixtures = [], []
    for match in upcoming:
        home = match.team1.team_id == oldb_team
        opponent = match.team2 if home else match.team1
        factor = config.home_factor if home else config.away_factor
        strength = strengths.get(opponent.team_id, config.promoted_points_per_game / 3)
        difficulties.append(min(strength * factor, 1.0))
        label = names.get(opponent.team_id) or opponent.short_name or opponent.name
        fixtures.append(f"{label} ({'H' if home else 'A'})")
    return Programme(value=1 - sum(difficulties) / len(difficulties), fixtures=tuple(fixtures))


async def load_programmes(
    client: OpenLigaDbClient,
    season: int,
    next_matchday: int,
    kickbase_teams: dict[str, str],
    config: ProgrammeConfig,
    now: datetime,
) -> Programmes:
    """`season` and `next_matchday` come from Kickbase: OpenLigaDB's "current" matchday does
    not say which season it belongs to (wrong in the summer break)."""
    if next_matchday > LAST_MATCHDAY:
        return Programmes(
            by_team={},
            fetched_at=now,
            stale=False,
            missing={team: "Saison beendet" for team in kickbase_teams},
        )
    table = await client.table(season)
    previous = await client.table(season - 1, ttl=PREVIOUS_SEASON_TTL)
    for name, rows in (("Tabelle", table.data), ("Vorjahrestabelle", previous.data)):
        if len(rows) != BUNDESLIGA_TEAMS:
            raise OpenLigaDbError(f"OpenLigaDB: {name} hat {len(rows)} statt 18 Vereine")
    fetched: list[Fetched] = [table, previous]
    matches: list[OldbMatch] = []
    for matchday in range(next_matchday, min(next_matchday + config.matches, LAST_MATCHDAY) + 1):
        day = await client.matchday(season, matchday)
        fetched.append(day)
        matches += day.data
    if not matches:
        raise OpenLigaDbError(f"OpenLigaDB: keine Spiele ab Spieltag {next_matchday}")

    mapping, missing = map_teams(kickbase_teams, table.data, load_team_mapping())
    strengths = team_strengths(table.data, previous.data, config)
    # Show opponents with the Kickbase names the rest of the app uses.
    names = {oldb: kickbase_teams[kb] for kb, oldb in mapping.items()}
    by_team = {}
    for kb_team, oldb_team in mapping.items():
        programme = programme_for(oldb_team, matches, strengths, names, config, now)
        if programme is None:
            missing[kb_team] = "keine Spiele in den nächsten Wochen"
        else:
            by_team[kb_team] = programme
    return Programmes(
        by_team=by_team,
        fetched_at=min(f.fetched_at for f in fetched),
        stale=any(f.stale for f in fetched),
        missing=missing,
    )
