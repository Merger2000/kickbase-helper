import os
from datetime import timedelta

import pytest

from app.db.models import Base
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.models import Position
from app.services.holding_roles import Role, roles_for, set_role
from app.services.sell_guide import SalePlayer, build_sell_guide
from tests.fakes import NOW
from tests.test_player_pages import PID, htmx_csrf_headers


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    url = (
        f"sqlite+pysqlite:///{tmp_path / 'roles.db'}"
        if request.param == "sqlite"
        else os.environ.get("TEST_POSTGRES_URL")
    )
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


async def test_roles_are_idempotent_reversible_and_scoped(sessions):
    await set_role(sessions, "league", "1", Role.CORE, NOW)
    await set_role(sessions, "league", "1", Role.CORE, NOW)
    await set_role(sessions, "other", "1", Role.TRADE, NOW)
    assert await roles_for(sessions, "league") == {"1": "core"}
    await set_role(sessions, "league", "1", Role.TRADE, NOW + timedelta(hours=1))
    assert await roles_for(sessions, "league") == {"1": "trade"}
    await set_role(sessions, "league", "1", Role.AUTO, NOW)
    assert await roles_for(sessions, "league") == {}
    assert await roles_for(sessions, "other") == {"1": "trade"}


def test_financing_never_sells_manual_core_and_keeps_full_roster_for_slot_penalty():
    core = SalePlayer("Keeper", 10_000_000, 100, 90, Position.GOALKEEPER, False, "1")
    extra = SalePlayer("Extra", 1_000_000, 5, 10, Position.FORWARD, False, "2")
    guide = build_sell_guide(-2_000_000, [core, extra], [core, extra], protected_ids={"1"})
    assert guide.best is None
    assert not guide.fewest.covers
    assert guide.fewest.surplus == -1_000_000
    assert {p.id for p in guide.fewest.players} == {"2"}
    assert {p.id for p in guide.candidates.players} == {"2"}
    guide = build_sell_guide(-500_000, [core, extra], [core, extra], protected_ids={"1"})
    assert guide.best.covers
    assert guide.best.newly_empty == 1  # protected keeper still belongs to the actual roster


def test_same_name_does_not_bypass_id_based_protection():
    squad = [SalePlayer("Müller", 10, id="1"), SalePlayer("Müller", 20, id="2")]
    guide = build_sell_guide(-5, squad, squad, protected_ids={"2"})
    assert [p.id for p in guide.best.players] == ["1"]


def test_all_players_protected_keeps_deficit_visible():
    squad = [SalePlayer("Core", 10, id="1")]
    guide = build_sell_guide(-5, squad, squad, protected_ids={"1"})
    assert guide.deficit == 5
    assert guide.best is None and guide.fewest.players == ()


def test_role_save_requires_login_and_csrf(client, logged_in_client):
    assert (
        logged_in_client.post("/kader/rolle", data={"player_id": PID, "role": "core"}).status_code
        == 403
    )


def test_save_and_clear_own_role(logged_in_client):
    headers = htmx_csrf_headers(logged_in_client)
    response = logged_in_client.post(
        "/kader/rolle", data={"player_id": PID, "role": "core"}, headers=headers
    )
    assert response.status_code == 200
    assert "Gespeichert." in response.text
    assert 'value="core" selected' in response.text
    assert 'value="core" selected' in logged_in_client.get("/kader/rollen").text
    response = logged_in_client.post(
        "/kader/rolle", data={"player_id": PID, "role": "auto"}, headers=headers
    )
    assert response.status_code == 200
    assert "Eigener Schutz aufgehoben" in response.text


def test_unknown_player_and_invalid_role_rejected(logged_in_client):
    headers = htmx_csrf_headers(logged_in_client)
    assert (
        logged_in_client.post(
            "/kader/rolle", data={"player_id": "99999999", "role": "core"}, headers=headers
        ).status_code
        == 409
    )
    assert (
        logged_in_client.post(
            "/kader/rolle", data={"player_id": PID, "role": "bad"}, headers=headers
        ).status_code
        == 422
    )


def test_database_failure_is_not_reported_as_saved(logged_in_client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import app.services.holding_roles as service

    async def fail(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(service, "set_role", fail)
    response = logged_in_client.post(
        "/kader/rolle",
        data={"player_id": PID, "role": "core"},
        headers=htmx_csrf_headers(logged_in_client),
    )
    assert response.status_code == 503
    assert "Gespeichert." not in response.text


def test_trading_reads_core_role_and_simulation_warns_explicit_sale(logged_in_client):
    logged_in_client.post(
        "/kader/rolle",
        data={"player_id": PID, "role": "core"},
        headers=htmx_csrf_headers(logged_in_client),
    )
    trading = logged_in_client.get("/empfehlungen/trading")
    assert trading.status_code == 200
    assert "Von dir geschützt" in trading.text
    sell = logged_in_client.get("/empfehlungen/verkaufen")
    assert "Geschützte Stammspieler werden im Finanzierungsplan ausgelassen" in sell.text
    simulation = logged_in_client.get("/empfehlungen/simulation", params={"verkauf": PID})
    assert "Dein Plan verkauft geschützte Stammspieler" in simulation.text
    assert (
        "Nach deinem Plan" in simulation.text
    )  # user's explicit hypothetical sale remains possible


def test_role_page_and_save_require_normal_login(client):
    assert client.get("/kader/rollen", follow_redirects=False).status_code == 303
    assert (
        client.post(
            "/kader/rolle", data={"player_id": PID, "role": "core"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_stale_roster_cannot_change_role(logged_in_client, monkeypatch, app):
    from app.kickbase.client import Fetched
    from app.kickbase.models import Squad
    from tests.fakes import fixture

    async def stale(*args, **kwargs):
        return Fetched(Squad.model_validate(fixture("league_squad")), NOW, True)

    monkeypatch.setattr(app.state.kickbase, "squad", stale)
    response = logged_in_client.post(
        "/kader/rolle",
        data={"player_id": PID, "role": "core"},
        headers=htmx_csrf_headers(logged_in_client),
    )
    assert response.status_code == 503
    assert "Rolle wurde nicht geändert" in response.text
