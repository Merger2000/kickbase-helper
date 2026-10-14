import asyncio

import httpx
import pytest

from app.kickbase.errors import KickbaseUnavailable
from app.kickbase.league import LEAGUE_KV_KEY, LeagueContext, LeagueNotFound, resolve_league
from tests.fakes import FakeKickbase, MemoryKV, fixture, make_client


@pytest.fixture
def fake() -> FakeKickbase:
    fake = FakeKickbase()
    fake.on("GET", "/v4/leagues/selection", fixture("leagues_selection"))
    return fake


async def test_resolves_league_by_name_and_caches_it(fake):
    kv = MemoryKV()
    async with make_client(fake) as client:
        league = await resolve_league(client, kv, "Testliga")
        again = await resolve_league(client, kv, "Testliga")
    assert league == again
    assert league.competition_id == "1"
    assert kv.data[LEAGUE_KV_KEY]["id"] == league.id
    assert len(fake.gets()) == 1


async def test_unknown_league_lists_available_names(fake):
    async with make_client(fake) as client:
        with pytest.raises(LeagueNotFound) as exc_info:
            await resolve_league(client, MemoryKV(), "Gibt es nicht")
    message = str(exc_info.value)
    assert "Testliga" in message
    assert "KICKBASE_LEAGUE_NAME" in message


async def test_never_falls_back_to_first_league(fake):
    async with make_client(fake) as client:
        with pytest.raises(LeagueNotFound):
            await resolve_league(client, MemoryKV(), "")


async def test_cache_for_other_name_is_ignored(fake):
    kv = MemoryKV()
    kv.data[LEAGUE_KV_KEY] = {"id": "1", "name": "Alte Liga", "competition_id": "1"}
    async with make_client(fake) as client:
        league = await resolve_league(client, kv, "Testliga")
    assert league.name == "Testliga"


async def test_context_resolves_once_for_concurrent_requests(fake):
    async with make_client(fake) as client:
        context = LeagueContext(client, MemoryKV(), "Testliga")
        results = await asyncio.gather(*(context.get() for _ in range(5)))
    assert len(set(results)) == 1
    assert len(fake.gets()) == 1


async def test_ambiguous_league_name_is_an_error():
    fake = FakeKickbase()
    data = fixture("leagues_selection")
    data["it"].append(dict(data["it"][1], i="999"))  # second "Testliga"
    fake.on("GET", "/v4/leagues/selection", data)
    kv = MemoryKV()
    async with make_client(fake) as client:
        with pytest.raises(LeagueNotFound, match="mehrfach"):
            await resolve_league(client, kv, "Testliga")
    assert kv.data == {}


async def test_cached_league_in_old_format_is_resolved_again(fake):
    kv = MemoryKV()
    kv.data[LEAGUE_KV_KEY] = {"id": "1", "name": "Testliga", "competition_id": "1", "new": 1}
    async with make_client(fake) as client:
        league = await resolve_league(client, kv, "Testliga")
    assert league.id != "1"
    assert len(fake.gets()) == 1


async def test_context_recovers_after_failure(fake):
    fake.on("GET", "/v4/leagues/selection", httpx.Response(503), fixture("leagues_selection"))
    async with make_client(fake, retry_delays=()) as client:
        context = LeagueContext(client, MemoryKV(), "Testliga")
        with pytest.raises(KickbaseUnavailable):
            await context.get()
        assert (await context.get()).name == "Testliga"


async def test_invalidate_forces_fresh_lookup(fake):
    kv = MemoryKV()
    async with make_client(fake, refresh_min_age=0) as client:
        context = LeagueContext(client, kv, "Testliga")
        await context.get()
        context.invalidate()
        await context.get()
    assert len(fake.gets()) == 2  # second lookup bypassed kv and response cache
