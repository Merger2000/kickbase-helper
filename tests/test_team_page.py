from datetime import UTC, datetime

import httpx
from sqlalchemy.exc import OperationalError

from app.db.models import MatchdayResult
from tests.conftest import extract_csrf
from tests.fakes import TEAM_LEAGUE_ID


def test_team_page_shows_scoreboard_pitch_and_squad(logged_in_client):
    html = logged_in_client.get("/").text
    assert "17,01" in html  # budget
    assert "3-5-2" in html
    assert html.count('class="token token--empty"') == 2
    assert "2 Plätze frei" in html
    assert "Hansen" in html and "Paderborn" in html
    assert "angeschlagen" in html
    assert 'id="refresh"' in html
    assert "Stand " in html


def test_refresh_requires_csrf_header(logged_in_client):
    assert logged_in_client.post("/team/refresh").status_code == 403
    response = logged_in_client.post(
        "/team/refresh", headers={"X-CSRF-Token": "falsch", "HX-Request": "true"}
    )
    assert response.status_code == 403


def test_refresh_returns_partial(logged_in_client):
    csrf = extract_csrf_header(logged_in_client)
    response = logged_in_client.post(
        "/team/refresh", headers={"X-CSRF-Token": csrf, "HX-Request": "true"}
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="team" id="team">')
    assert "<html" not in response.text


def test_kickbase_outage_shows_message_instead_of_error(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Kickbase ist gerade nicht erreichbar" in response.text
    assert 'class="scoreboard"' not in response.text


def test_rejected_login_is_explained(logged_in_client, fake_kickbase):
    fake_kickbase.on_login(httpx.Response(401))
    response = logged_in_client.get("/")
    assert "Login abgelehnt" in response.text


def test_unknown_league_lists_available_leagues(app, logged_in_client):
    from app.kickbase.league import LeagueContext

    app.state.league = LeagueContext(app.state.kickbase, app.state.league._kv, "Gibt es nicht")
    response = logged_in_client.get("/")
    assert "Gibt es nicht" in response.text
    assert "Testliga" in response.text


def test_forbidden_league_endpoint_resets_league(app, logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", httpx.Response(403))
    logged_in_client.get("/")
    assert app.state.league._league is None


def test_database_error_is_explained(app, logged_in_client, monkeypatch):
    async def broken():
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(app.state.league, "get", broken)
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Datenbank ist gerade nicht erreichbar" in response.text


def test_player_images_allowed_by_csp(logged_in_client):
    csp = logged_in_client.get("/").headers["content-security-policy"]
    assert "img-src 'self' data: https://kickbase.b-cdn.net" in csp


def extract_csrf_header(client) -> str:
    html = client.get("/").text
    marker = '"X-CSRF-Token": "'
    start = html.index(marker) + len(marker)
    return html[start : html.index('"', start)]


def test_login_form_csrf_still_works(client):
    assert extract_csrf(client.get("/login").text)


def htmx_headers(client) -> dict:
    return {"X-CSRF-Token": extract_csrf_header(client), "HX-Request": "true"}


def test_refresh_really_fetches_again(app, logged_in_client, fake_kickbase):
    from tests.fakes import install_fake_kickbase

    install_fake_kickbase(app, fake_kickbase, refresh_min_age=0)
    headers = htmx_headers(logged_in_client)  # loads the page once
    squad = f"/v4/leagues/{TEAM_LEAGUE_ID}/squad"
    before = len(fake_kickbase.gets(squad))
    logged_in_client.get("/")
    assert len(fake_kickbase.gets(squad)) == before  # plain page view: cache
    logged_in_client.post("/team/refresh", headers=headers)
    assert len(fake_kickbase.gets(squad)) == before + 1


def test_failed_refresh_keeps_swap_target_and_explains(app, logged_in_client, fake_kickbase):
    from tests.fakes import install_fake_kickbase

    install_fake_kickbase(app, fake_kickbase, refresh_min_age=0)
    headers = htmx_headers(logged_in_client)
    # 404 is not served from the cache (500/503 would show the cached data marked as old)
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/ranking", httpx.Response(404))
    response = logged_in_client.post("/team/refresh", headers=headers)
    assert response.status_code == 200
    assert '<div class="team" id="team">' in response.text
    assert 'class="alert"' in response.text


def test_stale_data_is_marked_on_the_page(app, logged_in_client, fake_kickbase):
    from tests.fakes import FakeClock, install_fake_kickbase

    clock = FakeClock()
    install_fake_kickbase(app, fake_kickbase, clock=clock)
    logged_in_client.get("/")
    clock.advance(minutes=20)
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
    html = logged_in_client.get("/").text
    assert "freshness--stale" in html
    assert "ältere Daten" in html


def test_logged_out_htmx_refresh_redirects_to_login(client):
    response = client.post("/team/refresh", headers={"HX-Request": "true"})
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/login"


def test_stale_csrf_on_htmx_reloads_page(logged_in_client):
    response = logged_in_client.post(
        "/team/refresh", headers={"X-CSRF-Token": "alt", "HX-Request": "true"}
    )
    assert response.status_code == 403
    assert response.headers["HX-Refresh"] == "true"


def test_missing_credentials_are_explained(app, logged_in_client, fake_kickbase):
    from app.secret_files import SecretError
    from tests.fakes import install_fake_kickbase

    def credentials():
        raise SecretError("fehlt")

    install_fake_kickbase(app, fake_kickbase, credentials=credentials)
    assert "Zugangsdaten fehlen" in logged_in_client.get("/").text


def test_unexpected_response_shows_generic_message(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", {"it": [{"pos": 1}]})
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "konnten nicht geladen werden" in response.text


def test_persistent_401_is_explained(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", httpx.Response(401))
    assert "lehnt den Zugang ab" in logged_in_client.get("/").text


def test_not_found_resets_league_but_server_error_does_not(app, logged_in_client, fake_kickbase):
    path = f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget"
    fake_kickbase.on("GET", path, httpx.Response(500))
    logged_in_client.get("/")
    assert app.state.league._league is not None
    fake_kickbase.on("GET", path, httpx.Response(404))
    logged_in_client.get("/")
    assert app.state.league._league is None


def test_read_only_violation_is_never_swallowed(app, logged_in_client, monkeypatch):
    import pytest

    from app.kickbase.errors import ReadOnlyViolation

    async def broken():
        raise ReadOnlyViolation("POST", "/v4/leagues/1/market")

    monkeypatch.setattr(app.state.league, "get", broken)
    with pytest.raises(ReadOnlyViolation):
        logged_in_client.get("/")


def test_guessed_formation_is_labelled(logged_in_client):
    assert "(vermutet)" in logged_in_client.get("/").text


def test_template_edge_cases(logged_in_client, fake_kickbase):
    from tests.fakes import fixture

    squad = fixture("league_squad")
    squad["it"][0].pop("pim")
    squad["it"][0]["tid"] = "999"  # club not in the table
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    budget = fixture("league_budget")
    budget["pbaa"] = budget["b"]
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    html = logged_in_client.get("/").text
    assert "None" not in html
    assert ", Ø" not in html.split(squad["it"][0]["n"], 2)[2][:200]  # no stray comma
    assert "nach Geboten" not in html
    assert html.count('class="avatar__img"') == 2 * 9 - 2  # pitch + list, one player lacks it


def test_league_names_are_escaped(app, logged_in_client, fake_kickbase):
    from app.kickbase.league import LeagueContext
    from tests.fakes import MemoryKV, fixture

    leagues = fixture("leagues_selection")
    leagues["it"][0]["n"] = "<script>alert(1)</script>"
    fake_kickbase.on("GET", "/v4/leagues/selection", leagues)
    app.state.league = LeagueContext(app.state.kickbase, MemoryKV(), "Gibt es nicht")
    html = logged_in_client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_team_page_without_open_offers(logged_in_client, fake_kickbase):
    from tests.fakes import fixture

    budget = fixture("league_budget")
    budget.pop("pbaa")
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    html = logged_in_client.get("/").text
    assert 'class="scoreboard"' in html
    assert "nach Geboten" not in html


def test_team_page_has_two_column_wrappers(logged_in_client):
    # Stacked on phones: roster stays with the lineup, news has its own column on desktop.
    html = logged_in_client.get("/").text
    main, side = html.index('class="team__main"'), html.index('class="team__side"')
    assert main < html.index('id="lineup-title"') < html.index('id="squad-title"') < side


# ---------------------------------------------------------------------------- matchday history (C3.4)  # noqa: E501


async def _seed_matchday_results(app, *rows: MatchdayResult) -> None:
    sessions = app.state.sessions
    async with sessions.begin() as session:
        for row in rows:
            session.add(row)


def test_matchday_history_empty_shows_message_only(logged_in_client, app):
    html = logged_in_client.get("/").text
    assert "Noch keine Spieltags-Bilanz gespeichert" in html
    assert "matchday-history__latest" not in html
    assert 'id="chart-placement"' not in html
    assert "matchday-chart-data" not in html


async def test_matchday_history_one_entry_shows_summary_no_chart(logged_in_client, app):
    await _seed_matchday_results(
        app,
        MatchdayResult(
            league_id=TEAM_LEAGUE_ID,
            season=2026,
            matchday=1,
            points=42,
            placement=5,
            matchday_placement=4,
            team_value=180_000_000,
            created_at=datetime.now(UTC),
        ),
    )
    html = logged_in_client.get("/").text
    assert 'class="matchday-history__latest"' in html
    assert "1. Spieltag" in html
    assert "42 Punkte" in html
    assert "Platz <strong>5</strong>" in html
    assert "Spieltagsplatz 4" in html
    # The chart scripts load as soon as any matchday exists so the HTMX refresh can
    # render the diagram once a second row arrives. The chart itself is still hidden
    # below the 2-row threshold.
    assert "vendor/chart.umd.min.js" in html
    assert "js/charts.js" in html
    assert 'id="chart-placement"' not in html
    assert "matchday-chart-data" not in html
    # First matchday: no placement delta yet.
    assert "▲" not in html and "▼" not in html


async def test_matchday_history_multiple_entries_render_chart_data(logged_in_client, app):
    await _seed_matchday_results(
        app,
        MatchdayResult(
            league_id=TEAM_LEAGUE_ID,
            season=2026,
            matchday=1,
            points=42,
            placement=5,
            matchday_placement=4,
            team_value=180_000_000,
            created_at=datetime.now(UTC),
        ),
        MatchdayResult(
            league_id=TEAM_LEAGUE_ID,
            season=2026,
            matchday=2,
            points=71,
            placement=3,
            matchday_placement=2,
            team_value=181_000_000,
            created_at=datetime.now(UTC),
        ),
    )
    html = logged_in_client.get("/").text
    # Chart canvas and the data script the front-end renders.
    assert 'id="chart-placement"' in html
    assert 'id="matchday-chart-data"' in html
    # Latest summary with placement delta (5 → 3: up by 2).
    assert "2. Spieltag" in html
    assert "71 Punkte" in html
    assert "Platz <strong>3</strong>" in html
    assert "▲2" in html
    # Table fallback lists rows in reverse matchday order.
    assert "Werte als Tabelle" in html
    table = html.split("Werte als Tabelle", 1)[1]
    assert table.index("2.") < table.index("1.")
