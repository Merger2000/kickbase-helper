"""LigaInsider's „voraussichtliche Aufstellung": the predicted starting eleven of a club.

The page only carries the formation (11 players), no bench and no „fehlt/verletzt" section.
Player names are matched to Kickbase players later (app/news/matching.py). A changed page
structure or a network problem raises FormatError instead of silently returning nothing.
Fetched gently: at most one request per `min_interval` seconds.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "ligainsider.yaml"
SOURCE = "ligainsider"
USER_AGENT = "kickbase-helper/1.0 (privates Dashboard, 1 Abruf pro Verein und Lauf)"
INJURIES_PATH = "/bundesliga/verletzte-und-gesperrte-spieler/"


class FormatError(Exception):
    pass


class LigainsiderConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    teams: dict[str, str]
    wappen_aliases: dict[str, str] = {}

    @property
    def team_by_slug(self) -> dict[str, str]:
        """Crest/URL club slug → Kickbase team id."""
        result = {slug.split("/")[0]: team_id for team_id, slug in self.teams.items()}
        result.update(self.wappen_aliases)
        return result


@lru_cache
def load_config(path: Path = CONFIG_PATH) -> LigainsiderConfig:
    return LigainsiderConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


STARTING = "startelf"
ALTERNATIVE = "alternative"  # the other candidate in a position battle (hidden on the page)


@dataclass(frozen=True)
class LineupPlayer:
    name: str
    status: str = STARTING


@dataclass(frozen=True)
class ParsedLineup:
    players: list[LineupPlayer]  # the starting eleven, in row order


@dataclass(frozen=True)
class Injury:
    name: str
    kind: str  # verletzung / aufbautraining / gesperrt / nicht_im_kader / ...
    reason: str | None  # e.g. "Kreuzbandriss", may be empty
    since: str | None  # e.g. "7 Monaten und 1 Woche", may be empty


@dataclass(frozen=True)
class TeamInjuries:
    slug: str  # club slug from the crest image URL
    entries: list[Injury]


@dataclass(frozen=True)
class ParsedInjuries:
    teams: list[TeamInjuries]


MIN_INTERVAL = 2.0  # seconds between two requests to the same domain (scraping rule)


class DomainPacer:
    """Keeps at least `min_interval` seconds between requests to one domain – across all
    clients and jobs of the process, and for every redirect hop."""

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self, _request: httpx.Request | None = None) -> None:
        async with self._lock:
            if self._last is not None:
                pause = self._last + self.min_interval - self._monotonic()
                if pause > 0:
                    await self._sleep(pause)
            self._last = self._monotonic()


_PACERS: dict[str, DomainPacer] = {}


def shared_pacer(host: str) -> DomainPacer:
    """The one pacer of a domain in this process."""
    return _PACERS.setdefault(host, DomainPacer())


class LigaInsiderClient:
    def __init__(
        self,
        config: LigainsiderConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pacer: DomainPacer | None = None,
    ) -> None:
        self.config = config or load_config()
        self.pacer = pacer or shared_pacer(httpx.URL(self.config.base_url).host)
        self._http = httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [self.pacer.wait]},  # also paces redirect hops
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def url_for(self, team_id: str) -> str:
        try:
            return f"/{self.config.teams[team_id]}/"
        except KeyError:
            raise FormatError(f"Keine LigaInsider-Zuordnung für Team {team_id}") from None

    async def fetch_lineup(self, team_id: str) -> ParsedLineup:
        return parse_lineup(await self._get(self.url_for(team_id)))

    async def fetch_injuries(self) -> ParsedInjuries:
        return parse_injuries(await self._get(INJURIES_PATH))

    async def _get(self, url: str) -> str:
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise FormatError(f"LigaInsider nicht erreichbar ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise FormatError(f"LigaInsider antwortet mit HTTP {response.status_code}")
        return response.text


def parse_lineup(html: str) -> ParsedLineup:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.stadium_container_bg")
    if root is None:
        raise FormatError("Aufstellung nicht gefunden (Seitenstruktur geändert?)")
    players = []
    for column in root.select("div[class*='player_position_row'] div.player_position_column"):
        name_a = _player_link(column)
        if name_a is None:
            continue
        name = " ".join(name_a.get_text(" ", strip=True).split())
        if name:
            players.append(LineupPlayer(name))
        # Position battle: the hidden candidates are LigaInsider's alternatives.
        for link in _alternative_links(column):
            alternative = " ".join(link.get_text(" ", strip=True).split())
            if alternative and alternative != name:
                players.append(LineupPlayer(alternative, ALTERNATIVE))
    if not players:
        raise FormatError("Aufstellung ohne Spieler, Seitenstruktur geändert?")
    if (
        sum(p.status == STARTING for p in players) < 7
    ):  # a real formation has ~11 players; fewer means a broken parse
        raise FormatError(f"Aufstellung unvollständig ({len(players)} Spieler)")
    return ParsedLineup(players)


_ICON_KINDS = {
    "verletzung": "verletzung",
    "krankheit": "krankheit",
    "aufbautraining": "aufbautraining",
    "angeschlagen": "angeschlagen",
    "angeschlagen-down": "angeschlagen",  # "Schwerer angeschlagen"
    "verbannung": "nicht_im_kader",
    "sperre": "gesperrt",
    "gelbsperre": "gesperrt",
    "gelb-rote-sperre": "gesperrt",
    "rotsperre": "gesperrt",
}


def parse_injuries(html: str) -> ParsedInjuries:
    """The injured/suspended players, grouped by club. A changed structure raises FormatError."""
    soup = BeautifulSoup(html, "html.parser")
    teams: list[TeamInjuries] = []
    for block in soup.select("div.personal_table"):
        slug = _club_slug(block)
        if slug is None:
            continue
        entries = []
        for row in block.select("div.small_table_row"):
            if row.select_one("div.small_table_column_noentry"):
                continue  # "Derzeit keine verletzten oder gesperrten Spieler"
            name = _text_of(row, "div.small_table_column1 .left_title strong")
            if not name:
                continue
            entries.append(
                Injury(
                    name=name,
                    kind=_injury_kind(row),
                    reason=_span_or_none(row, "div.small_table_column2"),
                    since=_span_or_none(row, "div.small_table_column4"),
                )
            )
        if entries:
            teams.append(TeamInjuries(slug, entries))
    if not teams:
        raise FormatError("Verletzten-Liste leer oder nicht lesbar, Seitenstruktur geändert?")
    return ParsedInjuries(teams)


def _club_slug(block: Tag) -> str | None:
    """The club slug from the crest image URL (`.../teams/medium/<slug>-wappen.png`)."""
    img = block.select_one("div.leg_table_title img")
    src = img.get("src") if img else None
    if not src or "/teams/" not in src:
        return None
    return src.rsplit("/", 1)[-1].split("-wappen")[0]


def _injury_kind(row: Tag) -> str:
    img = row.select_one("div.left_icon img")
    src = (img.get("src") or "") if img else ""
    filename = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if filename in _ICON_KINDS:
        return _ICON_KINDS[filename]
    alt = " ".join((img.get("alt") or "").split()) if img else ""
    normalized = alt.lower().replace(" ", "_")
    if normalized in _ICON_KINDS.values():
        return normalized
    logger.warning("LigaInsider: unbekanntes Ausfall-Symbol %r (alt %r)", filename, alt)
    return "unbekannt"


def _span_or_none(row: Tag, selector: str) -> str | None:
    span = row.select_one(f"{selector} span")
    text = " ".join(span.get_text(" ", strip=True).replace("\u00ad", "").split()) if span else ""
    return text or None


def _text_of(root: Tag, selector: str) -> str:
    node = root.select_one(selector)
    return " ".join(node.get_text(" ", strip=True).replace("\u00ad", "").split()) if node else ""


def _alternative_links(column: Tag) -> list[Tag]:
    """Player links of the hidden alternatives in a position battle."""
    links = []
    for sub in column.select(":scope > div.sub_child"):
        if "display:none" in (sub.get("style") or "").lower().replace(" ", ""):
            link = sub.select_one("div.player_name > a[itemprop='athlete']")
            if link is not None:
                links.append(link)
    return links


def _player_link(column: Tag) -> Tag | None:
    """The active player of a column: the visible alternative (`sub_child` without
    `display: none`) or, without alternatives, the plain player link. Returns None when the
    active player can't be determined (e.g. all alternatives hidden) instead of guessing."""
    sub_children = column.select(":scope > div.sub_child")
    if sub_children:
        for sub in sub_children:
            if "display:none" not in (sub.get("style") or "").lower().replace(" ", ""):
                return sub.select_one("div.player_name > a[itemprop='athlete']")
        return None  # all alternatives hidden: don't guess
    return column.select_one("div.player_name > a[itemprop='athlete']")
