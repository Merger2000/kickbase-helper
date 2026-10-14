import os
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db.models import Base
from app.db.session import make_engine, make_sessionmaker
from app.db.stores import DbResponseCache, DbTokenStore, KeyValueStore
from app.kickbase.stores import CacheEntry, Token

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
BACKENDS = ["sqlite", "postgres"]


@pytest.fixture(params=BACKENDS)
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'stores.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def test_token_roundtrip(sessions):
    store = DbTokenStore(sessions)
    assert await store.load() is None
    await store.save(Token("t1", NOW))
    await store.save(Token("t2", NOW + timedelta(days=7)))
    token = await store.load()
    assert token == Token("t2", NOW + timedelta(days=7))
    assert token.expires_at.tzinfo is not None
    await store.clear()
    assert await store.load() is None


async def test_cache_upsert_and_purge(sessions):
    cache = DbResponseCache(sessions)
    payload = {"it": [{"i": "1", "n": "Müller"}], "x": 1.5}
    await cache.set("/a", CacheEntry({"old": True}, NOW, NOW + timedelta(minutes=1)))
    await cache.set("/a", CacheEntry(payload, NOW, NOW + timedelta(minutes=10)))
    await cache.set("/b", CacheEntry([1, 2], NOW, NOW - timedelta(minutes=1)))
    entry = await cache.get("/a")
    assert entry == CacheEntry(payload, NOW, NOW + timedelta(minutes=10))
    assert await cache.purge_expired(NOW) == 1
    assert await cache.get("/b") is None
    assert await cache.get("/missing") is None


async def test_non_utc_datetimes_are_normalised(sessions):
    cache = DbResponseCache(sessions)
    berlin = timezone(timedelta(hours=2))
    await cache.set("/tz", CacheEntry({}, NOW.astimezone(berlin), NOW.astimezone(berlin)))
    entry = await cache.get("/tz")
    assert entry.fetched_at == NOW
    assert entry.fetched_at.utcoffset() == timedelta(0)


async def test_naive_datetimes_are_rejected(sessions):
    with pytest.raises(Exception, match="naive datetime"):
        await DbResponseCache(sessions).set("/x", CacheEntry({}, datetime(2026, 1, 1), NOW))


async def test_key_value_store(sessions):
    kv = KeyValueStore(sessions)
    assert await kv.get("k") is None
    await kv.set("k", {"id": "1"})
    await kv.set("k", {"id": "2", "name": "Testliga"})
    assert await kv.get("k") == {"id": "2", "name": "Testliga"}


async def test_client_end_to_end_with_database_stores(sessions):
    """Login, cache hit and stale fallback with the real stores and a large real payload."""
    import httpx

    from tests.fakes import FakeClock, FakeKickbase, fixture, make_client

    fake = FakeKickbase()
    path = "/v4/competitions/1/matchdays"
    fake.on("GET", path, fixture("competition_matchdays"))
    clock = FakeClock(NOW)
    tokens, cache = DbTokenStore(sessions), DbResponseCache(sessions)
    async with make_client(fake, tokens=tokens, cache=cache, clock=clock) as client:
        first = await client.competition_matchdays("1")
        second = await client.competition_matchdays("1")
    assert len(first.data.matchdays) == 34
    assert second.fetched_at == first.fetched_at
    assert (await tokens.load()).value == "token-1"

    clock.advance(hours=2)
    fake.on("GET", path, httpx.Response(503))
    async with make_client(fake, tokens=tokens, cache=cache, clock=clock) as client:
        stale = await client.competition_matchdays("1")
    assert stale.stale and stale.data == first.data
    assert fake.login_count == 1  # the second client reused the stored token


def test_engine_hides_sql_parameters(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'x.db'}")
    assert engine.sync_engine.hide_parameters is True
