"""Resolves the configured league ("Meine Liga") to its Kickbase id and caches it."""

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.kickbase.client import KickbaseClient
from app.kickbase.errors import KickbaseError

logger = logging.getLogger(__name__)

LEAGUE_KV_KEY = "kickbase.league"


class KeyValue(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any) -> None: ...


@dataclass(frozen=True)
class ResolvedLeague:
    id: str
    name: str
    competition_id: str


class LeagueNotFound(KickbaseError):
    def __init__(self, name: str, available: list[str], *, ambiguous: bool = False) -> None:
        listing = ", ".join(f"„{n}“" for n in available) or "keine"
        problem = "ist mehrfach vorhanden" if ambiguous else "wurde nicht gefunden"
        super().__init__(
            f"Liga „{name}“ {problem}. Ligen dieses Accounts: {listing}. "
            "KICKBASE_LEAGUE_NAME in .env prüfen."
        )
        self.name = name
        self.available = available


async def resolve_league(
    client: KickbaseClient, kv: KeyValue, league_name: str, *, refresh: bool = False
) -> ResolvedLeague:
    wanted = league_name.strip()
    if not refresh:
        cached = await kv.get(LEAGUE_KV_KEY)
        if isinstance(cached, dict) and str(cached.get("name", "")).strip() == wanted:
            try:
                return ResolvedLeague(**cached)
            except TypeError:
                logger.warning("Gespeicherte Liga hat ein altes Format, löse neu auf")

    leagues = (await client.leagues(refresh=refresh)).data.leagues
    matches = [league for league in leagues if league.name.strip() == wanted]
    if len(matches) != 1:
        raise LeagueNotFound(league_name, [lg.name for lg in leagues], ambiguous=bool(matches))
    match = matches[0]
    league = ResolvedLeague(id=match.id, name=match.name, competition_id=match.competition_id)
    await kv.set(LEAGUE_KV_KEY, asdict(league))
    logger.info("Liga „%s“ aufgelöst: id %s", league.name, league.id)
    return league


class LeagueContext:
    """Lazily resolved league, shared by all requests of a process."""

    def __init__(self, client: KickbaseClient, kv: KeyValue, league_name: str) -> None:
        self._client = client
        self._kv = kv
        self._name = league_name
        self._league: ResolvedLeague | None = None
        self._refresh_next = False
        self._lock = asyncio.Lock()

    async def get(self) -> ResolvedLeague:
        if self._league is None:
            async with self._lock:
                if self._league is None:
                    self._league = await resolve_league(
                        self._client, self._kv, self._name, refresh=self._refresh_next
                    )
                    self._refresh_next = False
        return self._league

    def invalidate(self) -> None:
        """Forget the league, e.g. after a 403/404 on a league endpoint; the next `get` looks it
        up again without using the cache."""
        self._league = None
        self._refresh_next = True
