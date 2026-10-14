"""Client for OpenLigaDB (public, no authentication). Only GET; responses are validated and cached
in the shared response cache under the prefix "openligadb:"."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from app.kickbase.client import Fetched
from app.kickbase.stores import CacheEntry, ResponseCache
from app.openligadb.models import OldbGroup, OldbMatch, OldbTableRow

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openligadb.de"
CACHE_PREFIX = "openligadb:"
MATCHES = TypeAdapter(list[OldbMatch])
TABLE = TypeAdapter(list[OldbTableRow])


class OpenLigaDbError(Exception):
    pass


class OpenLigaDbSchemaError(OpenLigaDbError):
    """The response does not have the expected format (API change, not an outage)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class OpenLigaDbClient:
    def __init__(
        self,
        cache: ResponseCache,
        *,
        league: str = "bl1",
        ttl: int = 3600,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._cache = cache
        self._league = league
        self._ttl = ttl
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            transport=transport,
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json", "User-Agent": "kickbase-helper/0.1"},
        )

    async def __aenter__(self) -> "OpenLigaDbClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get[T](self, path: str, parse: Callable[[Any], T], ttl: int) -> Fetched[T]:
        key = CACHE_PREFIX + path
        now = self._clock()
        entry = await self._cache.get(key)
        if entry is not None and entry.expires_at > now:
            try:
                return Fetched(parse(entry.payload), entry.fetched_at)
            except ValidationError:
                logger.info("Cache-Eintrag %s passt nicht mehr zum Modell, lade neu", key)
        try:
            response = await self._http.get(path)
            response.raise_for_status()
            payload = response.json()
            data = parse(payload)
        except ValidationError as exc:
            # A changed format is a bug to fix, not an outage to wait out.
            logger.error("OpenLigaDB: unerwartetes Format bei GET %s: %s", path, exc)
            raise OpenLigaDbSchemaError(f"OpenLigaDB: unerwartetes Format bei GET {path}") from exc
        except (httpx.HTTPError, ValueError) as exc:  # ValueError: invalid JSON
            if entry is not None:
                logger.warning("OpenLigaDB nicht verfügbar (%s), nutze Cache-Stand", exc)
                try:
                    return Fetched(parse(entry.payload), entry.fetched_at, stale=True)
                except ValidationError:
                    pass
            if isinstance(exc, httpx.HTTPStatusError):
                reason = f"HTTP {exc.response.status_code}"
            else:
                reason = type(exc).__name__
            logger.warning("OpenLigaDB: GET %s fehlgeschlagen: %r", path, exc)
            raise OpenLigaDbError(f"OpenLigaDB nicht erreichbar, {reason}") from exc
        await self._cache.set(key, CacheEntry(payload, now, now + timedelta(seconds=ttl)))
        return Fetched(data, now)

    async def current_matchday(self) -> Fetched[OldbGroup]:
        return await self._get(
            f"/getcurrentgroup/{self._league}", OldbGroup.model_validate, self._ttl
        )

    async def matchday(self, season: int, matchday: int) -> Fetched[list[OldbMatch]]:
        return await self._get(
            f"/getmatchdata/{self._league}/{season}/{matchday}", MATCHES.validate_python, self._ttl
        )

    async def table(self, season: int, *, ttl: int | None = None) -> Fetched[list[OldbTableRow]]:
        return await self._get(
            f"/getbltable/{self._league}/{season}", TABLE.validate_python, ttl or self._ttl
        )
