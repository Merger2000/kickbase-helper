import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Player, Team
from tests.fakes import fixture

PLAYER = fixture("competition_player")
PID = PLAYER["i"]


def add_player_endpoints(fake):
    fake.on("GET", "/v4/competitions/1/matchdays", fixture("competition_matchdays"))
    fake.on("GET", f"/v4/competitions/1/players/{PID}", PLAYER)
    fake.on(
        "GET",
        f"/v4/competitions/1/players/{PID}/marketValue/92",
        fixture("competition_player_market_value_92"),
    )
    fake.on(
        "GET",
        f"/v4/competitions/1/players/{PID}/performance",
        fixture("competition_player_performance"),
    )


def seed_players(settings, updated=None):
    from datetime import UTC, datetime

    updated = updated or datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(Team(id="29", name="Paderborn", image=None, updated_at=updated))
        session.add(
            Player(
                id=PID,
                name="Hansen",
                team_id="29",
                position=2,
                status=2,
                market_value=3_722_776,
                image=None,
                updated_at=updated,
            )
        )
    engine.dispose()


def test_players_page_empty_state(logged_in_client):
    html = logged_in_client.get("/spieler").text
    assert "Noch keine Spielerdaten" in html
    assert 'aria-current="page"' in html and ">Spieler<" in html


def test_players_page_lists_and_filters(settings, logged_in_client):
    seed_players(settings)
    html = logged_in_client.get("/spieler").text
    assert f'href="/spieler/{PID}"' in html
    assert "angeschlagen" in html
    assert "Keine Spieler gefunden" in logged_in_client.get("/spieler?q=gibtsnicht").text
    assert "Hansen" in logged_in_client.get("/spieler?pos=2").text
    assert "Hansen" not in logged_in_client.get("/spieler?pos=4").text


def test_players_search_returns_partial_for_htmx(settings, logged_in_client):
    seed_players(settings)
    response = logged_in_client.get("/spieler?q=han", headers={"HX-Request": "true"})
    assert response.text.lstrip().startswith('<div class="player-results"')
    assert "<html" not in response.text


def test_players_query_is_validated(logged_in_client):
    assert logged_in_client.get("/spieler?pos=9").status_code == 422
    assert logged_in_client.get("/spieler?pos=x").status_code == 422
    assert logged_in_client.get("/spieler?q=" + "x" * 61).status_code == 422


def test_all_positions_filter_sends_empty_or_zero(settings, logged_in_client):
    seed_players(settings)
    for query in ("?q=han&pos=", "?q=han&pos=0", "?q=han"):
        response = logged_in_client.get("/spieler" + query, headers={"HX-Request": "true"})
        assert response.status_code == 200, query
        assert "Hansen" in response.text


def test_players_list_shows_data_age(settings, logged_in_client):
    seed_players(settings)
    html = logged_in_client.get("/spieler").text
    assert "Marktwerte und Status vom" in html
    assert "überfällig" not in html


def test_players_list_warns_when_sync_is_overdue(settings, logged_in_client):
    from datetime import UTC, datetime, timedelta

    seed_players(settings, updated=datetime.now(UTC) - timedelta(hours=40))
    assert "überfällig" in logged_in_client.get("/spieler").text


def test_player_page_renders_charts_and_tables(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    response = logged_in_client.get(f"/spieler/{PID}")
    html = response.text
    assert response.status_code == 200
    assert 'id="chart-market-value"' in html and 'id="chart-points"' in html
    assert 'id="chart-data"' in html
    assert "Werte als Tabelle" in html
    assert "Nächste Spiele" in html
    assert "chart.umd.min.js" in html
    assert "Verlauf direkt von Kickbase" in html  # nothing collected in the test DB


def test_player_page_chart_data_is_safe_json(logged_in_client, fake_kickbase):
    import json
    import re

    add_player_endpoints(fake_kickbase)
    html = logged_in_client.get(f"/spieler/{PID}").text
    raw = re.search(r'<script type="application/json" id="chart-data">(.*?)</script>', html, re.S)
    data = json.loads(raw.group(1))
    assert len(data["days"]) == len(data["values"]) == 92
    assert data["matchdays"] == [1, 2, 3, 4]


def test_404_of_other_endpoints_is_not_unknown_player(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    fake_kickbase.on("GET", "/v4/competitions/1/matchdays", httpx.Response(404))
    response = logged_in_client.get(f"/spieler/{PID}")
    assert response.status_code == 200
    assert "gibt es bei Kickbase nicht" not in response.text
    assert "konnten nicht geladen werden" in response.text


def test_unknown_player_is_404_without_resetting_league(app, logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    fake_kickbase.on("GET", "/v4/competitions/1/players/123", httpx.Response(404))
    response = logged_in_client.get("/spieler/123")
    assert response.status_code == 404
    assert "gibt es bei Kickbase nicht" in response.text
    assert app.state.league._league is not None


def test_player_id_must_be_numeric(logged_in_client):
    assert logged_in_client.get("/spieler/abc").status_code == 422
    assert logged_in_client.get("/spieler/1;drop").status_code == 422


def test_team_page_links_to_player_pages(logged_in_client):
    html = logged_in_client.get("/").text
    squad = fixture("league_squad")["it"]
    assert all(f'href="/spieler/{p["i"]}"' in html for p in squad)


def htmx_csrf_headers(client) -> dict:
    html = client.get("/").text
    marker = '"X-CSRF-Token": "'
    start = html.index(marker) + len(marker)
    token = html[start : html.index('"', start)]
    return {"X-CSRF-Token": token, "HX-Request": "true"}


class _FakeLlm:
    async def aclose(self):
        pass

    async def complete(self, task, system, user, schema_name, output):
        return type("C", (), {"data": type("D", (), {"text": "Ein starker Angreifer."})()})()


def test_player_ai_without_key(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    response = logged_in_client.post(
        f"/spieler/{PID}/ki", headers=htmx_csrf_headers(logged_in_client)
    )
    assert response.status_code == 200
    assert "kein API-Key konfiguriert" in response.text


def test_player_ai_with_fake_llm(app, logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    app.state.llm = _FakeLlm()
    response = logged_in_client.post(
        f"/spieler/{PID}/ki", headers=htmx_csrf_headers(logged_in_client)
    )
    assert response.status_code == 200
    assert "Ein starker Angreifer." in response.text


class _RaisingLlm:
    async def aclose(self):
        pass

    async def complete(self, task, system, user, schema_name, output):
        from app.llm.errors import LlmUnavailable

        raise LlmUnavailable("down")


def test_player_ai_handles_llm_error(app, logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    app.state.llm = _RaisingLlm()
    response = logged_in_client.post(
        f"/spieler/{PID}/ki", headers=htmx_csrf_headers(logged_in_client)
    )
    assert response.status_code == 200
    assert "nicht verfügbar" in response.text


def test_player_ai_requires_csrf(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    assert logged_in_client.post(f"/spieler/{PID}/ki").status_code == 403


HX = {"HX-Request": "true"}


def test_player_page_loads_the_decision_lazily(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert f'hx-get="/spieler/{PID}/einschaetzung"' in html


def test_decision_for_an_own_player_is_keep_or_sell(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    html = logged_in_client.get(f"/spieler/{PID}/einschaetzung", headers=HX).text
    assert "Halten oder verkaufen?" in html
    assert "Wie entsteht der Score?" in html  # the score parts stay explainable
    assert "Profil „verkaufen“" in html


def test_decision_for_another_player_is_buy(logged_in_client, fake_kickbase):
    import copy

    add_player_endpoints(fake_kickbase)
    other = copy.deepcopy(PLAYER)
    other["i"] = "999"
    fake_kickbase.on("GET", "/v4/competitions/1/players/999", other)
    for suffix in ("marketValue/92", "performance"):
        fake_kickbase.on(
            "GET",
            f"/v4/competitions/1/players/999/{suffix}",
            fixture(
                "competition_player_market_value_92"
                if suffix.startswith("market")
                else "competition_player_performance"
            ),
        )
    html = logged_in_client.get("/spieler/999/einschaetzung", headers=HX).text
    assert "Kaufen?" in html and "Mindest-Score" in html
    assert "Profil „kaufen“" in html


def test_decision_without_htmx_goes_to_the_player_page(logged_in_client):
    response = logged_in_client.get(f"/spieler/{PID}/einschaetzung", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/spieler/{PID}"


def test_decision_outage_is_a_note(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    fake_kickbase.on("GET", f"/v4/competitions/1/players/{PID}", httpx.Response(503))
    html = logged_in_client.get(f"/spieler/{PID}/einschaetzung", headers=HX).text
    assert "Die Einschätzung konnte nicht geladen werden" in html


def test_decision_shows_xp_value_and_breakdown(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    html = logged_in_client.get(f"/spieler/{PID}/einschaetzung", headers=HX).text
    # C4.4: value in the facts and the breakdown table on the decision box.
    assert "Aufstellungsindex" in html
    assert "Erwartete Punkte" not in html
    assert "/100" in html
    assert "% xP" not in html
    assert "Wie entsteht der Aufstellungsindex?" in html
    assert "Aufstellungsindex" in html


def test_decision_hides_xp_when_openligadb_is_down(logged_in_client, fake_kickbase, fake_oldb):
    add_player_endpoints(fake_kickbase)
    fake_oldb.down = True
    html = logged_in_client.get(f"/spieler/{PID}/einschaetzung", headers=HX).text
    # Without opponent data xP collapses to the neutral factor; the breakdown stays
    # visible so the missing-data hint explains the value.
    assert "Wie entsteht der Aufstellungsindex?" in html
    assert "Gegner" in html
    assert "neutral angesetzt" in html
