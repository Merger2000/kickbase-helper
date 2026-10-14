import asyncio
import logging
from datetime import timedelta

import httpx
import pytest

from app.kickbase.client import LOGIN_PATH, cache_key, ensure_allowed
from app.kickbase.errors import (
    KickbaseAuthError,
    KickbaseCredentialsMissing,
    KickbaseHTTPError,
    KickbaseLoginFailed,
    KickbaseSchemaError,
    KickbaseUnavailable,
    ReadOnlyViolation,
)
from app.kickbase.stores import CacheEntry, MemoryResponseCache, MemoryTokenStore, Token
from app.secret_files import SecretError
from tests.fakes import NOW, FakeClock, FakeKickbase, Monotonic, make_client

PATH = "/v4/user/me"
ME = {"u": {"id": "9000001", "name": "Manager 1"}}


@pytest.fixture
def fake() -> FakeKickbase:
    fake = FakeKickbase()
    fake.on("GET", PATH, ME)
    return fake


# ---------------------------------------------------------------- read-only guarantee


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v4/leagues/1/market/2/offers"),
        ("POST", "/v4/leagues/1/lineup"),
        ("POST", "/v4/user/login/"),  # only the exact login path is allowed
        ("PUT", "/v4/user/settings"),
        ("DELETE", "/v4/leagues/1/market/2"),
        ("PATCH", "/v4/user/me"),
        ("delete", "/v4/user"),
    ],
)
def test_only_get_and_login_are_allowed(method, path):
    with pytest.raises(ReadOnlyViolation):
        ensure_allowed(method, path)


@pytest.mark.parametrize(("method", "path"), [("GET", "/v4/anything"), ("POST", LOGIN_PATH)])
def test_allowed_requests(method, path):
    ensure_allowed(method, path)


async def test_write_request_never_leaves_the_client(fake):
    async with make_client(fake) as client:
        with pytest.raises(ReadOnlyViolation):
            await client._send("POST", "/v4/leagues/1/market/2/offers", json={"price": 1})
    assert fake.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_underlying_http_client_is_guarded_too(fake, method):
    async with make_client(fake) as client:
        with pytest.raises(ReadOnlyViolation):
            await client._http.request(method, "/v4/leagues/1/market")
    assert fake.requests == []


# ---------------------------------------------------------------- authentication


async def test_logs_in_once_and_reuses_token(fake):
    fake.on("GET", "/v4/leagues/selection", {"it": []})
    calls = []
    async with make_client(fake, credentials=lambda: calls.append(1) or ("a", "b")) as client:
        await client.get_json(PATH, ttl=60)
        await client.get_json("/v4/leagues/selection", ttl=60)
    assert fake.login_count == 1
    assert len(calls) == 1  # credentials are read only for the login
    assert all(r.headers["Authorization"] == "Bearer token-1" for r in fake.gets())


async def test_login_request_body(fake):
    async with make_client(fake, credentials=lambda: ("me@example.com", "geheim")) as client:
        await client.get_json(PATH, ttl=60)
    login = next(r for r in fake.requests if r.method == "POST")
    assert login.url.path == LOGIN_PATH
    assert login.read() == b'{"em":"me@example.com","pass":"geheim"}'


async def test_stored_token_is_reused_without_login(fake):
    tokens = MemoryTokenStore(Token("stored", NOW + timedelta(days=3)))
    async with make_client(fake, tokens=tokens) as client:
        await client.get_json(PATH, ttl=60)
    assert fake.login_count == 0
    assert fake.gets()[0].headers["Authorization"] == "Bearer stored"


async def test_token_close_to_expiry_triggers_login(fake):
    tokens = MemoryTokenStore(Token("old", NOW + timedelta(minutes=30)))
    async with make_client(fake, tokens=tokens) as client:
        await client.get_json(PATH, ttl=60)
    assert fake.login_count == 1
    assert tokens.token.value == "token-1"


async def test_401_triggers_exactly_one_relogin(fake):
    fake.on("GET", PATH, httpx.Response(401), ME)
    fake.on_login(fake.login_response("token-1"), fake.login_response("token-2"))
    tokens = MemoryTokenStore()
    async with make_client(fake, tokens=tokens) as client:
        result = await client.get_json(PATH, ttl=60)
    assert result.data == ME
    assert fake.login_count == 2
    assert [r.headers["Authorization"] for r in fake.gets()] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
    assert tokens.token.value == "token-2"


async def test_second_401_aborts_and_blocks(fake):
    fake.on("GET", PATH, httpx.Response(401))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseAuthError):
            await client.get_json(PATH, ttl=60)
        with pytest.raises(KickbaseAuthError):
            await client.get_json(PATH, ttl=60, refresh=True)
    assert fake.login_count == 2  # initial login + exactly one re-login, then nothing
    assert len(fake.gets()) == 2


def reject_old_token(request: httpx.Request) -> httpx.Response:
    if request.headers["Authorization"] == "Bearer old":
        return httpx.Response(401)
    return httpx.Response(200, json={"path": request.url.path})


async def test_concurrent_401s_share_one_relogin():
    fake = FakeKickbase(delay=0.01)
    paths = [f"/v4/competitions/1/players/{i}" for i in range(3)]
    for path in paths:
        fake.on("GET", path, reject_old_token)
    fake.on_login(fake.login_response("token-1"), fake.login_response("token-2"))
    tokens = MemoryTokenStore(Token("old", NOW + timedelta(days=3)))
    async with make_client(fake, tokens=tokens, max_parallel=2) as client:
        await asyncio.gather(*(client.get_json(p, ttl=60) for p in paths))
    assert fake.login_count == 1
    assert tokens.token.value == "token-1"


async def test_relogin_reuses_token_saved_by_other_process():
    """App and worker share the token store: after A logged in again, B's 401 must pick up
    A's new token instead of logging in a third time (and must not delete it)."""
    fake = FakeKickbase()
    fake.on("GET", PATH, ME)
    shared = MemoryTokenStore(Token("old", NOW + timedelta(days=3)))
    fake.on_login(fake.login_response("token-new"))
    async with make_client(fake, tokens=shared) as app, make_client(fake, tokens=shared) as worker:
        await app.get_json(PATH, ttl=60)  # both start with "old"
        await worker.get_json(PATH, ttl=60, params={"w": 1})
        fake.on("GET", PATH, httpx.Response(401), ME)
        await app.get_json(PATH, ttl=60, params={"a": 2})  # 401 -> login -> token-new
        fake.on("GET", PATH, httpx.Response(401), ME)
        await worker.get_json(PATH, ttl=60, params={"w": 2})  # 401 -> token-new from store
    assert fake.login_count == 1
    assert shared.token.value == "token-new"
    assert fake.gets()[-1].headers["Authorization"] == "Bearer token-new"


async def test_rejected_login_cools_down(fake):
    fake.on_login(httpx.Response(401, json={"err": 1, "errMsg": "AccessDenied"}))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseLoginFailed):
            await client.get_json(PATH, ttl=60)
        with pytest.raises(KickbaseLoginFailed):
            await client.get_json(PATH, ttl=60)
    assert len([r for r in fake.requests if r.method == "POST"]) == 1


async def test_login_without_token_field_blocks_further_logins(fake):
    fake.on_login(httpx.Response(200, json={"token": "wrong-field"}))
    async with make_client(fake) as client:
        for _ in range(2):
            with pytest.raises(KickbaseSchemaError):
                await client.get_json(PATH, ttl=60)
    assert fake.login_count == 1


async def test_login_server_error_is_transient_without_cooldown(fake):
    fake.on_login(httpx.Response(503), fake.login_response("token-1"))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseUnavailable):
            await client.get_json(PATH, ttl=60)
        assert (await client.get_json(PATH, ttl=60)).data == ME
    assert fake.login_count == 2


async def test_unexpected_login_status_is_permanent_error(fake):
    fake.on_login(httpx.Response(404, json={"err": 1, "errMsg": "NotFound"}))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseHTTPError, match=r"POST /v4/user/login.*NotFound"):
            await client.get_json(PATH, ttl=60)


async def test_missing_secret_sends_no_login(fake):
    def credentials():
        raise SecretError("Secret 'kickbase_password' ist fehlt")

    async with make_client(fake, credentials=credentials) as client:
        with pytest.raises(KickbaseCredentialsMissing):
            await client.get_json(PATH, ttl=60)
    assert fake.requests == []


async def test_login_cooldown_expires(fake):
    clock = Monotonic()
    fake.on_login(httpx.Response(401), fake.login_response("token-1"))
    async with make_client(fake, monotonic=clock, login_cooldown=900) as client:
        with pytest.raises(KickbaseLoginFailed):
            await client.get_json(PATH, ttl=60)
        clock.advance(899)
        with pytest.raises(KickbaseLoginFailed):
            await client.get_json(PATH, ttl=60)
        clock.advance(2)
        assert (await client.get_json(PATH, ttl=60)).data == ME
    assert fake.login_count == 2


async def test_token_and_password_never_logged(fake, caplog):
    fake.on("GET", PATH, httpx.Response(401), ME)
    fake.on_login(fake.login_response("token-secret-1"), fake.login_response("token-secret-2"))
    with caplog.at_level(logging.DEBUG):
        async with make_client(fake, credentials=lambda: ("me@example.com", "pw-123")) as client:
            await client.get_json(PATH, ttl=60)
    for secret in ("token-secret-1", "token-secret-2", "pw-123", "me@example.com"):
        assert secret not in caplog.text


# ---------------------------------------------------------------- cache


async def test_cache_hit_avoids_request(fake):
    async with make_client(fake) as client:
        first = await client.get_json(PATH, ttl=600)
        second = await client.get_json(PATH, ttl=600)
    assert len(fake.gets()) == 1
    assert second.data == first.data
    assert second.fetched_at == first.fetched_at
    assert not second.stale


async def test_expired_entry_is_refetched(fake):
    clock = FakeClock()
    async with make_client(fake, clock=clock) as client:
        await client.get_json(PATH, ttl=600)
        clock.advance(seconds=601)
        await client.get_json(PATH, ttl=600)
    assert len(fake.gets()) == 2


async def test_refresh_respects_minimum_age(fake):
    clock = FakeClock()
    async with make_client(fake, clock=clock) as client:
        await client.get_json(PATH, ttl=600)
        clock.advance(seconds=30)
        await client.get_json(PATH, ttl=600, refresh=True)
        assert len(fake.gets()) == 1  # younger than 60 s: no new request
        clock.advance(seconds=31)
        await client.get_json(PATH, ttl=600, refresh=True)
    assert len(fake.gets()) == 2


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_errors_are_retried(fake, status):
    fake.on("GET", PATH, httpx.Response(status), httpx.Response(status), ME)
    async with make_client(fake) as client:
        result = await client.get_json(PATH, ttl=60)
    assert result.data == ME
    assert len(fake.gets()) == 3


async def test_network_errors_are_retried_then_raised(fake):
    fake.on("GET", PATH, httpx.ConnectError("down"))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseUnavailable):
            await client.get_json(PATH, ttl=60)
    assert len(fake.gets()) == 3


async def test_stale_cache_served_when_kickbase_is_down(fake):
    clock = FakeClock()
    cache = MemoryResponseCache()
    stale_time = NOW - timedelta(hours=2)
    await cache.set(PATH, CacheEntry(ME, stale_time, stale_time + timedelta(minutes=10)))
    fake.on("GET", PATH, httpx.Response(503))
    async with make_client(fake, clock=clock, cache=cache) as client:
        result = await client.get_json(PATH, ttl=600)
    assert result.stale
    assert result.fetched_at == stale_time
    assert result.data == ME


async def test_unexpected_status_is_not_cached(fake):
    fake.on("GET", PATH, httpx.Response(404), ME)
    async with make_client(fake) as client:
        with pytest.raises(KickbaseHTTPError) as exc_info:
            await client.get_json(PATH, ttl=600)
        assert exc_info.value.status_code == 404
        assert (await client.get_json(PATH, ttl=600)).data == ME


async def test_invalid_response_is_not_cached_and_keeps_good_entry(fake):
    clock = FakeClock()
    fake.on("GET", "/v4/leagues/selection", {"it": []}, {"unexpected": 1}, {"it": []})
    async with make_client(fake, clock=clock) as client:
        good = await client.leagues()
        clock.advance(seconds=86401)  # expired
        with pytest.raises(KickbaseSchemaError, match="/v4/leagues/selection"):
            await client.leagues()
        again = await client.leagues()
    assert again.data == good.data
    assert len(fake.gets()) == 3  # the broken body was not served from the cache


async def test_non_json_response_is_schema_error(fake):
    fake.on("GET", PATH, httpx.Response(200, text="<html>Wartung</html>"))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseSchemaError, match="Keine JSON-Antwort"):
            await client.get_json(PATH, ttl=60)


async def test_cached_entry_from_older_model_is_refetched(fake):
    cache = MemoryResponseCache()
    path = "/v4/leagues/selection"
    await cache.set(path, CacheEntry({"old": "shape"}, NOW, NOW + timedelta(hours=1)))
    fake.on("GET", path, {"it": []})
    async with make_client(fake, cache=cache) as client:
        assert (await client.leagues()).data.leagues == []
    assert len(fake.gets()) == 1


async def test_worker_mode_never_serves_stale(fake):
    cache = MemoryResponseCache()
    stale_time = NOW - timedelta(hours=2)
    await cache.set(PATH, CacheEntry(ME, stale_time, stale_time + timedelta(minutes=10)))
    fake.on("GET", PATH, httpx.Response(503))
    async with make_client(fake, cache=cache, serve_stale=False) as client:
        with pytest.raises(KickbaseUnavailable):
            await client.get_json(PATH, ttl=600)


async def test_identical_concurrent_requests_share_one_call():
    fake = FakeKickbase(delay=0.01)
    fake.on("GET", PATH, ME)
    async with make_client(fake, max_parallel=2) as client:
        results = await asyncio.gather(*(client.get_json(PATH, ttl=60) for _ in range(4)))
    assert len(fake.gets()) == 1
    assert all(r.data == ME for r in results)


async def test_retry_backoff_delays(fake):
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    fake.on("GET", PATH, httpx.Response(429), httpx.Response(429), ME)
    async with make_client(fake, retry_delays=(1.0, 3.0), sleep=record) as client:
        await client.get_json(PATH, ttl=60)
    assert waits == [1.0, 3.0]


async def test_long_retry_after_gives_up_immediately(fake):
    fake.on("GET", PATH, httpx.Response(429, headers={"Retry-After": "120"}))
    async with make_client(fake, retry_delays=(1.0, 3.0)) as client:
        with pytest.raises(KickbaseUnavailable, match="Retry-After"):
            await client.get_json(PATH, ttl=60)
    assert len(fake.gets()) == 1


async def test_permanent_transport_errors_are_not_retried(fake):
    fake.on("GET", PATH, httpx.UnsupportedProtocol("nope"))
    async with make_client(fake) as client:
        with pytest.raises(httpx.UnsupportedProtocol):
            await client.get_json(PATH, ttl=60)
    assert len(fake.gets()) == 1


async def test_http_error_includes_kickbase_message(fake):
    fake.on("GET", PATH, httpx.Response(403, json={"err": 3, "errMsg": "NotAMember"}))
    async with make_client(fake) as client:
        with pytest.raises(KickbaseHTTPError, match="NotAMember"):
            await client.get_json(PATH, ttl=60)


def test_token_repr_hides_value():
    assert "geheim" not in repr(Token("geheim", NOW))


def test_cache_key_is_stable():
    assert cache_key("/x", None) == "/x"
    assert cache_key("/x", {"b": 2, "a": 1}) == "/x?a=1&b=2"
    assert cache_key("/x", {"a": None}) == "/x"


# ---------------------------------------------------------------- gentle access


@pytest.mark.parametrize("max_parallel", [1, 2])
async def test_parallel_requests_are_limited(max_parallel):
    fake = FakeKickbase(delay=0.01)
    paths = [f"/v4/competitions/1/players/{i}" for i in range(6)]
    for path in paths:
        fake.on("GET", path, {"i": path})
    async with make_client(fake, max_parallel=max_parallel) as client:
        await asyncio.gather(*(client.get_json(p, ttl=60) for p in paths))
    assert fake.max_in_flight == max_parallel


async def test_requests_are_paced(fake):
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    fake.on("GET", "/v4/leagues/selection", {"it": []})
    async with make_client(fake, min_interval=0.5, sleep=record, monotonic=Monotonic()) as client:
        await client.get_json(PATH, ttl=60)
        await client.get_json("/v4/leagues/selection", ttl=60)
    assert waits == [0.5, 0.5]  # login, then each GET waits the full gap (clock stands still)


async def test_outage_backoff_serves_cache_without_new_attempts(fake):
    clock = FakeClock()
    mono = Monotonic()
    cache = MemoryResponseCache()
    old = NOW - timedelta(hours=1)
    other = "/v4/leagues/selection"
    await cache.set(other, CacheEntry({"it": []}, old, old + timedelta(minutes=10)))
    fake.on("GET", PATH, httpx.Response(503))
    async with make_client(
        fake, clock=clock, cache=cache, monotonic=mono, unavailable_backoff=60
    ) as client:
        with pytest.raises(KickbaseUnavailable):
            await client.get_json(PATH, ttl=60)
        attempts = len(fake.gets())
        stale = await client.get_json(other, ttl=60)  # within backoff: cache, no request
        assert stale.stale and len(fake.gets()) == attempts
        mono.advance(59)
        await client.get_json(other, ttl=60)  # still cache; must not extend the backoff
        mono.advance(2)
        fake.on("GET", other, {"it": []})
        assert not (await client.get_json(other, ttl=60)).stale
