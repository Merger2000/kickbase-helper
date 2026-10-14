"""Read-only client for the (unofficial) Kickbase API v4.

Hard rules (CLAUDE.md): only GET requests plus `POST /v4/user/login`; anything else raises
`ReadOnlyViolation` before it leaves the process. Requests are serialised and paced, responses
validated and cached, and the token is reused until shortly before it expires. On a 401 the
client logs in again exactly once; a second 401 raises `KickbaseAuthError` and blocks further
requests for a cooldown period.
"""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ValidationError

from app.kickbase import models as m
from app.kickbase.errors import (
    KickbaseAuthError,
    KickbaseCredentialsMissing,
    KickbaseError,
    KickbaseHTTPError,
    KickbaseLoginFailed,
    KickbaseSchemaError,
    KickbaseUnavailable,
    ReadOnlyViolation,
)
from app.kickbase.stores import CacheEntry, ResponseCache, Token, TokenStore
from app.secret_files import SecretError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.kickbase.com"
LOGIN_PATH = "/v4/user/login"
USER_AGENT = "kickbase-helper/0.1 (private dashboard, read-only)"
TOKEN_MARGIN = timedelta(hours=1)
# Transport errors worth retrying; others (e.g. invalid header values) are permanent.
RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

Credentials = Callable[[], tuple[str, str]]


def ensure_allowed(method: str, path: str) -> None:
    method = method.upper()
    if method == "GET" or (method == "POST" and path == LOGIN_PATH):
        return
    raise ReadOnlyViolation(method, path)


async def _guard_request(request: httpx.Request) -> None:
    # Second safety net: also applies if someone uses the underlying httpx client directly.
    ensure_allowed(request.method, request.url.path)


def utcnow() -> datetime:
    return datetime.now(UTC)


def cache_key(path: str, params: dict[str, Any] | None) -> str:
    if not params:
        return path
    clean = {k: v for k, v in sorted(params.items()) if v is not None}
    return f"{path}?{urlencode(clean)}" if clean else path


@dataclass(frozen=True)
class CacheTTL:
    live: int = 600
    player: int = 1800
    static: int = 86400


@dataclass(frozen=True)
class Fetched[T]:
    """A response plus when it was fetched. `stale` marks an expired cache entry that was
    served because Kickbase was unreachable."""

    data: T
    fetched_at: datetime
    stale: bool = False


def _same_token(token: Token, other: Token | None) -> bool:
    return other is not None and token.value == other.value


def _parse_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _error_detail(response: httpx.Response) -> str | None:
    """Kickbase error bodies look like {"err": 1, "errMsg": "AccessDenied"}."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("errMsg"), str):
        return body["errMsg"][:100]
    return None


def _json_body(response: httpx.Response, method: str, path: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        content_type = response.headers.get("content-type", "?")
        raise KickbaseSchemaError(
            f"Keine JSON-Antwort auf {method} {path} "
            f"({content_type}, {len(response.content)} Bytes)"
        ) from exc


class KickbaseClient:
    def __init__(
        self,
        credentials: Credentials,
        tokens: TokenStore,
        cache: ResponseCache,
        *,
        ttl: CacheTTL | None = None,
        max_parallel: int = 1,
        min_interval: float = 0.25,
        refresh_min_age: float = 60.0,
        retry_delays: tuple[float, ...] = (1.0, 3.0),
        login_cooldown: float = 900.0,
        unavailable_backoff: float = 60.0,
        serve_stale: bool = True,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._credentials = credentials
        self._tokens = tokens
        self._cache = cache
        self.ttl = ttl or CacheTTL()
        self.max_parallel = max_parallel
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._min_interval = min_interval
        self._refresh_min_age = timedelta(seconds=refresh_min_age)
        self._retry_delays = retry_delays
        self._login_cooldown = login_cooldown
        # After an outage, skip further attempts for a while instead of waiting for timeouts
        # on every endpoint of a page.
        self._unavailable_backoff = unavailable_backoff
        self._unavailable_until = float("-inf")
        # The worker turns this off: its time series must never record old data as new.
        self._serve_stale = serve_stale
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._pace_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        # One lock per cache key: identical concurrent requests share one API call.
        self._key_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request = float("-inf")
        self._blocked: tuple[float, KickbaseError] | None = None
        self._token: Token | None = None
        self.request_count = 0  # requests actually sent (for job statistics)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            event_hooks={"request": [_guard_request]},
            follow_redirects=False,
        )

    async def __aenter__(self) -> "KickbaseClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport

    async def _pace(self) -> None:
        async with self._pace_lock:
            wait = self._last_request + self._min_interval - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
            self._last_request = self._monotonic()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        ensure_allowed(method, path)
        async with self._semaphore:
            await self._pace()
            started = self._monotonic()
            self.request_count += 1
            response = await self._http.request(method, path, **kwargs)
            logger.debug(
                "Kickbase %s %s -> %s (%.0f ms)",
                method,
                path,
                response.status_code,
                (self._monotonic() - started) * 1000,
            )
            return response

    # ------------------------------------------------------------------ auth

    def _block(self, error: KickbaseError) -> None:
        """Stop talking to Kickbase for `login_cooldown` seconds after an auth failure, so a
        rejected login or a persistent 401 never turns into one login per request."""
        self._blocked = (self._monotonic() + self._login_cooldown, error)

    def _raise_if_blocked(self) -> None:
        if self._blocked is None:
            return
        until, error = self._blocked
        remaining = until - self._monotonic()
        if remaining <= 0:
            self._blocked = None
            return
        raise type(error)(f"{error} Nächster Versuch in {remaining:.0f} s.")

    def _usable(self, token: Token) -> bool:
        return token.usable(self._clock(), TOKEN_MARGIN)

    async def _current_token(self) -> Token:
        if self._token is not None and self._usable(self._token):
            return self._token
        return await self._login()

    async def _login(self, rejected: Token | None = None) -> Token:
        async with self._login_lock:
            # Another task, or the other container via the shared store, may have logged in
            # while we waited for the lock. Use that token instead of logging in again.
            for candidate in (self._token, await self._tokens.load()):
                if (
                    candidate is not None
                    and not _same_token(candidate, rejected)
                    and self._usable(candidate)
                ):
                    self._token = candidate
                    return candidate
            self._raise_if_blocked()
            try:
                email, password = self._credentials()
            except SecretError as exc:
                raise KickbaseCredentialsMissing(
                    f"Kickbase-Zugangsdaten nicht verfügbar: {exc}"
                ) from None
            try:
                response = await self._send(
                    "POST", LOGIN_PATH, json={"em": email, "pass": password}
                )
            except RETRYABLE_ERRORS as exc:
                raise KickbaseUnavailable(f"Kickbase-Login nicht erreichbar: {exc!r}") from exc
            token = self._token_from_login(response)
            # Keep the token in memory first: a failing database write must not cost a login.
            self._token = token
            self._blocked = None
            await self._tokens.save(token)
            logger.info("Kickbase-Login erfolgreich, Token gültig bis %s", token.expires_at)
            return token

    def _token_from_login(self, response: httpx.Response) -> Token:
        status = response.status_code
        if status in (400, 401, 403):
            error = KickbaseLoginFailed(
                f"Kickbase lehnt den Login ab (HTTP {status}). "
                "Zugangsdaten in den Docker Secrets prüfen."
            )
            self._block(error)
            raise error
        if status == 429 or status >= 500:
            raise KickbaseUnavailable(f"Kickbase-Login: HTTP {status}")
        if status != 200:
            raise KickbaseHTTPError(
                status, LOGIN_PATH, method="POST", detail=_error_detail(response)
            )
        try:
            body = _json_body(response, "POST", LOGIN_PATH)
            value = body.get("tkn") if isinstance(body, dict) else None
            if not isinstance(value, str) or not value:
                raise KickbaseSchemaError("Login-Antwort enthält keinen Token (Feld 'tkn')")
        except KickbaseSchemaError as error:
            self._block(error)
            raise
        now = self._clock()
        expires_at = _parse_expiry(body.get("tknex"))
        if expires_at is None:
            logger.warning("Login-Antwort ohne lesbares Ablaufdatum, nehme 1 Tag an")
            expires_at = now + timedelta(days=1)
        elif expires_at <= now + TOKEN_MARGIN:
            logger.warning("Kickbase-Token läuft schon %s ab", expires_at)
        return Token(value=value, expires_at=expires_at)

    # ------------------------------------------------------------------ GET

    async def _get_with_retries(
        self, path: str, params: dict[str, Any] | None, token: Token
    ) -> httpx.Response:
        problem = ""
        max_delay = max(self._retry_delays, default=0.0)
        for delay in (0.0, *self._retry_delays):
            if delay:
                await self._sleep(delay)
            try:
                response = await self._send(
                    "GET", path, params=params, headers={"Authorization": f"Bearer {token.value}"}
                )
            except RETRYABLE_ERRORS as exc:
                problem = repr(exc)
                logger.warning("Kickbase GET %s fehlgeschlagen: %s", path, problem)
                continue
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit() and int(retry_after) > max_delay:
                    raise KickbaseUnavailable(
                        f"Kickbase drosselt Anfragen (Retry-After {retry_after} s): GET {path}"
                    )
            if response.status_code == 429 or response.status_code >= 500:
                problem = f"HTTP {response.status_code}"
                logger.warning("Kickbase GET %s: %s", path, problem)
                continue
            return response
        raise KickbaseUnavailable(f"Kickbase nicht erreichbar: GET {path} ({problem})")

    async def _fetch(self, path: str, params: dict[str, Any] | None) -> Any:
        self._raise_if_blocked()
        token = await self._current_token()
        response = await self._get_with_retries(path, params, token)
        if response.status_code == 401:
            logger.warning("Kickbase 401 auf GET %s, einmaliger Neu-Login", path)
            token = await self._login(rejected=token)
            response = await self._get_with_retries(path, params, token)
            if response.status_code == 401:
                error = KickbaseAuthError(f"Kickbase lehnt auch den neuen Token ab (GET {path}).")
                self._block(error)
                raise error
        if response.status_code != 200:
            raise KickbaseHTTPError(response.status_code, path, detail=_error_detail(response))
        return _json_body(response, "GET", path)

    async def get_json[T](
        self,
        path: str,
        *,
        ttl: int,
        params: dict[str, Any] | None = None,
        refresh: bool = False,
        parse: Callable[[Any], T] = lambda payload: payload,
    ) -> Fetched[T]:
        """GET with cache. The response is parsed (validated) before it is cached, so a broken
        response never replaces good data. `refresh` bypasses a fresh entry unless it is younger
        than `refresh_min_age`. On transient errors a stale entry is returned (marked `stale`)
        if the client serves stale data."""
        key = cache_key(path, params)
        async with self._key_locks[key]:
            now = self._clock()
            entry = await self._cache.get(key)
            cached = self._parse_cached(entry, parse, key)
            if cached is not None and entry is not None:
                fresh = entry.expires_at > now
                too_young_to_refresh = now - entry.fetched_at < self._refresh_min_age
                if (fresh and not refresh) or (refresh and too_young_to_refresh):
                    return Fetched(cached, entry.fetched_at)
            if self._monotonic() < self._unavailable_until:
                # Recent outage: answer from the cache (or fail) without a new attempt. This
                # must not extend the backoff, or Kickbase would never be asked again.
                return self._stale_or_raise(
                    KickbaseUnavailable(f"Kickbase war eben nicht erreichbar: GET {path}"),
                    cached,
                    entry,
                    key,
                )
            try:
                payload = await self._fetch(path, params)
            except KickbaseUnavailable as exc:
                self._unavailable_until = self._monotonic() + self._unavailable_backoff
                return self._stale_or_raise(exc, cached, entry, key)
            self._unavailable_until = float("-inf")
            try:
                data = parse(payload)
            except ValidationError as exc:
                raise KickbaseSchemaError(
                    f"Unerwartete Antwort auf GET {path}: {exc.error_count()} Feldfehler, "
                    f"zuerst {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}"
                ) from exc
            await self._cache.set(key, CacheEntry(payload, now, now + timedelta(seconds=ttl)))
            return Fetched(data, now)

    def _stale_or_raise[T](
        self, error: KickbaseUnavailable, cached: T | None, entry: CacheEntry | None, key: str
    ) -> Fetched[T]:
        if cached is None or entry is None or not self._serve_stale:
            raise error
        logger.warning(
            "Kickbase nicht erreichbar, nutze Cache-Stand %s für %s", entry.fetched_at, key
        )
        return Fetched(cached, entry.fetched_at, stale=True)

    @staticmethod
    def _parse_cached[T](entry: CacheEntry | None, parse: Callable[[Any], T], key: str) -> T | None:
        if entry is None:
            return None
        try:
            return parse(entry.payload)
        except ValidationError:
            # Cached under an older model version: treat as a cache miss.
            logger.info("Cache-Eintrag %s passt nicht mehr zum Modell, lade neu", key)
            return None

    # ------------------------------------------------------------------ typed endpoints

    async def _typed[M: BaseModel](
        self,
        model: type[M],
        path: str,
        *,
        ttl: int,
        params: dict[str, Any] | None = None,
        refresh: bool = False,
    ) -> Fetched[M]:
        return await self.get_json(
            path, ttl=ttl, params=params, refresh=refresh, parse=model.model_validate
        )

    # User and leagues
    async def user_me(self, *, refresh: bool = False) -> Fetched[m.UserMe]:
        return await self._typed(m.UserMe, "/v4/user/me", ttl=self.ttl.static, refresh=refresh)

    async def leagues(self, *, refresh: bool = False) -> Fetched[m.LeagueSelection]:
        return await self._typed(
            m.LeagueSelection, "/v4/leagues/selection", ttl=self.ttl.static, refresh=refresh
        )

    async def league_me(self, league_id: str, *, refresh: bool = False) -> Fetched[m.LeagueMe]:
        return await self._typed(
            m.LeagueMe, f"/v4/leagues/{league_id}/me", ttl=self.ttl.live, refresh=refresh
        )

    async def budget(self, league_id: str, *, refresh: bool = False) -> Fetched[m.Budget]:
        return await self._typed(
            m.Budget, f"/v4/leagues/{league_id}/me/budget", ttl=self.ttl.live, refresh=refresh
        )

    async def squad(self, league_id: str, *, refresh: bool = False) -> Fetched[m.Squad]:
        return await self._typed(
            m.Squad, f"/v4/leagues/{league_id}/squad", ttl=self.ttl.live, refresh=refresh
        )

    async def lineup(self, league_id: str, *, refresh: bool = False) -> Fetched[m.Lineup]:
        return await self._typed(
            m.Lineup, f"/v4/leagues/{league_id}/lineup", ttl=self.ttl.live, refresh=refresh
        )

    async def my_eleven(self, league_id: str, *, refresh: bool = False) -> Fetched[m.MyEleven]:
        return await self._typed(
            m.MyEleven,
            f"/v4/leagues/{league_id}/teamcenter/myeleven",
            ttl=self.ttl.live,
            refresh=refresh,
        )

    async def ranking(
        self, league_id: str, *, matchday: int | None = None, refresh: bool = False
    ) -> Fetched[m.Ranking]:
        # A specific matchday is immutable once finished; cache it for 24 h. The default
        # (latest) ranking keeps the 10-min live TTL because it is updated as matches finish.
        ttl = self.ttl.static if matchday is not None else self.ttl.live
        return await self._typed(
            m.Ranking,
            f"/v4/leagues/{league_id}/ranking",
            ttl=ttl,
            params={"dayNumber": matchday},
            refresh=refresh,
        )

    async def market(self, league_id: str, *, refresh: bool = False) -> Fetched[m.Market]:
        return await self._typed(
            m.Market, f"/v4/leagues/{league_id}/market", ttl=self.ttl.live, refresh=refresh
        )

    async def user_teamcenter(
        self,
        league_id: str,
        user_id: str,
        matchday: int,
        *,
        refresh: bool = False,
        live: bool = False,
    ) -> Fetched[m.UserTeamCenter]:
        """Own frozen lineup. Reports cache 24 h, provisional points at least 10 min.

        A previously cached report can still have a 24-h expiry. Check its age when
        requested live, without changing shared cache semantics for other endpoints.
        """
        path = f"/v4/leagues/{league_id}/users/{user_id}/teamcenter"
        ttl = max(600, self.ttl.live) if live else self.ttl.static
        result = await self._typed(
            m.UserTeamCenter,
            path,
            ttl=ttl,
            params={"dayNumber": matchday},
            refresh=refresh,
        )
        if (
            live
            and not result.stale
            and self._clock() - result.fetched_at >= timedelta(seconds=ttl)
        ):
            result = await self._typed(
                m.UserTeamCenter,
                path,
                ttl=ttl,
                params={"dayNumber": matchday},
                refresh=True,
            )
        return result

    async def transfers(
        self, league_id: str, *, start: int = 0, limit: int = 50, refresh: bool = False
    ) -> Fetched[m.ActivityFeed]:
        """Transfer activities of the league (newest first), one page."""
        return await self._typed(
            m.ActivityFeed,
            f"/v4/leagues/{league_id}/activitiesFeed",
            ttl=self.ttl.live,
            params={"start": start, "max": limit, "filter": m.TRANSFER_ACTIVITY},
            refresh=refresh,
        )

    # Players in the context of a league (includes owner information)
    async def league_player(
        self, league_id: str, player_id: str, *, refresh: bool = False
    ) -> Fetched[m.PlayerDetails]:
        return await self._typed(
            m.PlayerDetails,
            f"/v4/leagues/{league_id}/players/{player_id}",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def league_player_performance(
        self, league_id: str, player_id: str, *, refresh: bool = False
    ) -> Fetched[m.Performance]:
        return await self._typed(
            m.Performance,
            f"/v4/leagues/{league_id}/players/{player_id}/performance",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def league_player_market_value(
        self, league_id: str, player_id: str, timeframe: int = 92, *, refresh: bool = False
    ) -> Fetched[m.MarketValueHistory]:
        _check_timeframe(timeframe)
        return await self._typed(
            m.MarketValueHistory,
            f"/v4/leagues/{league_id}/players/{player_id}/marketValue/{timeframe}",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    # Competition (Bundesliga) data, independent of the league
    async def competitions(self, *, refresh: bool = False) -> Fetched[m.Competitions]:
        return await self._typed(
            m.Competitions, "/v4/competitions", ttl=self.ttl.static, refresh=refresh
        )

    async def competition_players(
        self,
        competition_id: str,
        *,
        position: int | None = None,
        sorting: str | None = None,
        refresh: bool = False,
    ) -> Fetched[m.CompetitionPlayers]:
        return await self._typed(
            m.CompetitionPlayers,
            f"/v4/competitions/{competition_id}/players",
            ttl=self.ttl.live,
            params={"position": position, "sorting": sorting},
            refresh=refresh,
        )

    async def competition_table(
        self, competition_id: str, *, refresh: bool = False
    ) -> Fetched[m.CompetitionTable]:
        return await self._typed(
            m.CompetitionTable,
            f"/v4/competitions/{competition_id}/table",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def competition_matchdays(
        self, competition_id: str, *, refresh: bool = False
    ) -> Fetched[m.Matchdays]:
        return await self._typed(
            m.Matchdays,
            f"/v4/competitions/{competition_id}/matchdays",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def competition_player(
        self, competition_id: str, player_id: str, *, refresh: bool = False
    ) -> Fetched[m.PlayerDetails]:
        return await self._typed(
            m.PlayerDetails,
            f"/v4/competitions/{competition_id}/players/{player_id}",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def competition_player_performance(
        self, competition_id: str, player_id: str, *, refresh: bool = False
    ) -> Fetched[m.Performance]:
        return await self._typed(
            m.Performance,
            f"/v4/competitions/{competition_id}/players/{player_id}/performance",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def competition_player_market_value(
        self, competition_id: str, player_id: str, timeframe: int = 92, *, refresh: bool = False
    ) -> Fetched[m.MarketValueHistory]:
        _check_timeframe(timeframe)
        return await self._typed(
            m.MarketValueHistory,
            f"/v4/competitions/{competition_id}/players/{player_id}/marketValue/{timeframe}",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def team_profile(
        self, competition_id: str, team_id: str, *, refresh: bool = False
    ) -> Fetched[m.TeamProfile]:
        return await self._typed(
            m.TeamProfile,
            f"/v4/competitions/{competition_id}/teams/{team_id}/teamprofile",
            ttl=self.ttl.player,
            refresh=refresh,
        )

    async def team_center(
        self, competition_id: str, team_id: str, matchday: int, *, refresh: bool = False
    ) -> Fetched[m.TeamCenter]:
        return await self._typed(
            m.TeamCenter,
            f"/v4/competitions/{competition_id}/teams/{team_id}/teamcenter",
            ttl=self.ttl.player,
            params={"dayNumber": matchday},
            refresh=refresh,
        )

    async def match_details(
        self, match_id: str, *, refresh: bool = False
    ) -> Fetched[m.MatchDetails]:
        return await self._typed(
            m.MatchDetails,
            f"/v4/matches/{match_id}/details",
            ttl=self.ttl.player,
            refresh=refresh,
        )


def _check_timeframe(timeframe: int) -> None:
    if timeframe not in (92, 365):
        raise ValueError("timeframe muss 92 oder 365 sein")
