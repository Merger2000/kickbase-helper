"""Storage and queries for the predicted starting elevens (LigaInsider)."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Injury, LineupPrediction, Team
from app.lineups.ligainsider import (
    ALTERNATIVE,
    SOURCE,
    FormatError,
    LineupPlayer,
    TeamInjuries,
    load_config,
)
from app.news.matching import KnownPlayer, match_player

logger = logging.getLogger(__name__)

# Scraped data older than this is ignored (a note says so) instead of used as current.
PREDICTION_MAX_AGE = timedelta(hours=36)
# KV keys with the outcome of the last scrapes (unreadable clubs, unknown crests).
LINEUPS_STATUS_KEY = "ligainsider.lineups"
INJURIES_STATUS_KEY = "ligainsider.injuries"


@dataclass(frozen=True)
class InjuryNote:
    kind: str
    reason: str | None
    since: str | None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


KIND_LABELS = {
    "verletzung": "Verletzt",
    "krankheit": "Krank",
    "aufbautraining": "Im Aufbautraining",
    "angeschlagen": "Angeschlagen",
    "gesperrt": "Gesperrt",
    "nicht_im_kader": "Nicht im Kader",
}


async def store_lineup(
    sessions: async_sessionmaker,
    team_id: str,
    players: list[LineupPlayer],
    known: list[KnownPlayer],
    now: datetime,
) -> tuple[int, int]:
    """Replace the team's previous prediction with the new one. Returns (stored, unmatched)."""
    if not players:
        return 0, 0  # never wipe the previous prediction with an empty one
    rows, unmatched = [], []
    for p in players:
        player_id = match_player(p.name, team_id, known)
        if player_id is None:
            unmatched.append(p.name)
        rows.append(
            LineupPrediction(
                source=SOURCE,
                team_id=team_id,
                player_id=player_id,
                player_name=p.name,
                fetched_at=now,
                status=p.status,
            )
        )
    async with sessions.begin() as session:
        await session.execute(
            delete(LineupPrediction).where(
                LineupPrediction.source == SOURCE, LineupPrediction.team_id == team_id
            )
        )
        session.add_all(rows)
    if unmatched:
        logger.warning(
            "LigaInsider (%s): %d nicht zugeordnet: %s",
            team_id,
            len(unmatched),
            ", ".join(unmatched),
        )
    return len(rows), len(unmatched)


@dataclass(frozen=True)
class Prognosis:
    """What the predicted eleven says about one player."""

    starting: bool | None = None  # True: in the eleven; False: not listed; None: no/unclear
    battle: bool = False  # the alternative in a position battle
    unmatched: tuple[str, ...] = ()  # names of his club that couldn't be matched


@dataclass(frozen=True)
class TeamPrediction:
    """The predicted eleven of one club: matched Kickbase ids (starters and position-battle
    alternatives) plus the names that could not be matched. While names are unmatched, a player
    who isn't listed may well be one of them, so his status is unknown instead of "not in the
    eleven"."""

    starting: frozenset[str]
    unmatched: tuple[str, ...] = ()
    alternatives: frozenset[str] = frozenset()

    def prognosis(self, player_id: str) -> Prognosis:
        if player_id in self.starting:
            return Prognosis(True, unmatched=self.unmatched)
        if player_id in self.alternatives:
            return Prognosis(None, battle=True, unmatched=self.unmatched)
        return Prognosis(None if self.unmatched else False, unmatched=self.unmatched)


def prediction_for(predicted: dict[str, TeamPrediction], team_id: str, player_id: str) -> Prognosis:
    """The prognosis for a player; an empty one when his club has no prediction."""
    team = predicted.get(team_id)
    return team.prognosis(player_id) if team is not None else Prognosis()


async def predicted_players(
    sessions: async_sessionmaker, now: datetime, team_id: str | None = None
) -> dict[str, TeamPrediction]:
    """Current predicted elevens per club (all clubs, or only `team_id`). A club whose
    prediction is older than PREDICTION_MAX_AGE (its page couldn't be read since) is left out:
    no prognosis rather than last week's one."""
    query = select(
        LineupPrediction.team_id,
        LineupPrediction.player_id,
        LineupPrediction.player_name,
        LineupPrediction.status,
    ).where(
        LineupPrediction.source == SOURCE,
        LineupPrediction.fetched_at >= now - PREDICTION_MAX_AGE,
    )
    if team_id is not None:
        query = query.where(LineupPrediction.team_id == team_id)
    async with sessions() as session:
        rows = (await session.execute(query)).all()
    starting: dict[str, set[str]] = {}
    alternatives: dict[str, set[str]] = {}
    unmatched: dict[str, list[str]] = {}
    for team, player_id, name, status in rows:
        starting.setdefault(team, set())
        if player_id is None:
            # An unmatched alternative can at most mean a position battle, never "in the
            # eleven": only unmatched starters make the club's prognosis unclear.
            if status != ALTERNATIVE:
                unmatched.setdefault(team, []).append(name)
        elif status == ALTERNATIVE:
            alternatives.setdefault(team, set()).add(player_id)
        else:
            starting[team].add(player_id)
    return {
        team: TeamPrediction(
            frozenset(ids),
            tuple(sorted(unmatched.get(team, ()))),
            frozenset(alternatives.get(team, ())),
        )
        for team, ids in starting.items()
    }


def _current_injuries(now: datetime):
    return (Injury.source == SOURCE, Injury.fetched_at >= now - PREDICTION_MAX_AGE)


async def injuries_for_players(
    sessions: async_sessionmaker, now: datetime
) -> dict[str, InjuryNote]:
    """The current injury/suspension per matched player id (an outdated list is ignored)."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Injury).where(*_current_injuries(now), Injury.player_id.is_not(None))
            )
        ).all()
    result: dict[str, InjuryNote] = {}
    for row in rows:
        injury = row[0]
        result[injury.player_id] = InjuryNote(injury.kind, injury.reason, injury.since)
    return result


async def ligainsider_hint(sessions: async_sessionmaker, kv, now: datetime) -> str | None:
    """Why the start chances may be incomplete (clubs without a current prediction, an outdated
    injury list, unreadable pages, unknown crests) – None when everything is current."""
    async with sessions() as session:
        latest = dict(
            (
                await session.execute(
                    select(LineupPrediction.team_id, func.max(LineupPrediction.fetched_at))
                    .where(LineupPrediction.source == SOURCE)
                    .group_by(LineupPrediction.team_id)
                )
            ).all()
        )
        injury_latest = await session.scalar(
            select(func.max(Injury.fetched_at)).where(Injury.source == SOURCE)
        )
        names = dict((await session.execute(select(Team.id, Team.name))).all())
    hints = []
    scraped = set(load_config().teams)  # clubs no longer scraped (relegated) don't count
    outdated = sorted(
        names.get(t, t)
        for t, at in latest.items()
        if t in scraped and now - at > PREDICTION_MAX_AGE
    )
    lineups = await kv.get(LINEUPS_STATUS_KEY) or {}
    unreadable = sorted(names.get(t, t) for t in lineups.get("unreadable", []))
    if outdated:
        hints.append(
            f"Keine aktuelle Startelf-Prognose für {', '.join(outdated)} (älter als 36 h)."
        )
    elif unreadable:
        hints.append(f"Startelf-Prognose zuletzt nicht lesbar für {', '.join(unreadable)}.")
    if injury_latest is not None and now - injury_latest > PREDICTION_MAX_AGE:
        hints.append("Die Verletztenliste ist älter als 36 h und wird nicht berücksichtigt.")
    skipped = (await kv.get(INJURIES_STATUS_KEY) or {}).get("skipped", [])
    if skipped:
        hints.append(
            f"Verletztenliste unvollständig: Verein(e) {', '.join(skipped)} nicht erkannt."
        )
    return " ".join(hints) or None


async def store_injuries(
    sessions: async_sessionmaker,
    teams: list[TeamInjuries],
    team_by_slug: dict[str, str],
    known: list[KnownPlayer],
    now: datetime,
) -> tuple[int, int, list[str]]:
    """Replace all injuries with the freshly scraped list. Returns (stored, unmatched, skipped
    club slugs – crests that weren't recognised)."""
    rows, unmatched, skipped = [], [], []
    for team in teams:
        team_id = team_by_slug.get(team.slug)
        if team_id is None:
            skipped.append(team.slug)
            logger.warning("LigaInsider Verletzte: Verein '%s' unbekannt, übersprungen", team.slug)
            continue
        for entry in team.entries:
            player_id = match_player(entry.name, team_id, known)
            if player_id is None:
                unmatched.append(entry.name)
            rows.append(
                Injury(
                    source=SOURCE,
                    team_id=team_id,
                    player_id=player_id,
                    player_name=entry.name,
                    kind=entry.kind,
                    reason=entry.reason,
                    since=entry.since,
                    fetched_at=now,
                )
            )
    if teams and len(skipped) == len(teams):
        # Nothing could be mapped: the club slugs must have changed. Keep the old data and fail.
        raise FormatError("Keine Vereine zugeordnet, Verletzten-Seite hat sich geändert?")
    async with sessions.begin() as session:
        await session.execute(delete(Injury).where(Injury.source == SOURCE))
        session.add_all(rows)
    if unmatched:
        logger.warning(
            "LigaInsider Verletzte: %d nicht zugeordnet: %s", len(unmatched), ", ".join(unmatched)
        )
    return len(rows), len(unmatched), skipped


async def injury_for_player(
    sessions: async_sessionmaker, player_id: str, now: datetime
) -> InjuryNote | None:
    """The player's current injury/suspension, or None (an outdated list is ignored)."""
    async with sessions() as session:
        row = (
            await session.execute(
                select(Injury)
                .where(*_current_injuries(now), Injury.player_id == player_id)
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    injury = row[0]
    return InjuryNote(injury.kind, injury.reason, injury.since)
