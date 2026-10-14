def test_plan_overview_exposes_tools_without_api_requests(logged_in_client, fake_kickbase):
    before = len(fake_kickbase.requests)
    response = logged_in_client.get("/empfehlungen", follow_redirects=False)
    assert response.status_code == 200
    assert len(fake_kickbase.requests) == before
    for path in [
        "kaufen",
        "verkaufen",
        "trading",
        "elf",
        "simulation",
        "bilanz",
        "gewinne",
        "trends",
    ]:
        assert f'href="/empfehlungen/{path}"' in response.text
    assert 'href="/spieltag"' in response.text
    assert 'href="/kader/rollen"' in response.text


def test_buy_page(logged_in_client):
    html = logged_in_client.get("/empfehlungen/kaufen").text
    assert 'aria-current="page">Kaufen<' in html
    assert "Wie entsteht der Score?" in html
    assert "config/scoring.yaml" in html
    assert "OpenLigaDB (ODbL)" in html
    assert 'aria-current="page">Plan<' in html


def test_sell_page_shows_reasons(logged_in_client):
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert "Hansen" in html and "Marktwert −" in html
    assert "Seit Kauf" in html


def test_eleven_page_shows_pitch_with_xp(logged_in_client):
    html = logged_in_client.get("/empfehlungen/elf").text
    assert 'class="pitch"' in html
    assert "/100" in html
    assert "% xP" not in html
    assert "Warum diese Elf?" in html
    assert "Wie entsteht der Aufstellungsindex?" in html


def test_eleven_page_index_has_honest_units_and_a_sum(logged_in_client):
    import re

    html = logged_in_client.get("/empfehlungen/elf").text
    total = re.search(r"Index-Summe\s*<strong[^>]*>(\d+)</strong>", html)
    assert total
    assert 0 <= int(total.group(1)) <= 1100
    values = re.findall(r'rec__total">(\d+)</span><span class="rec__unit">Index / 100</span>', html)
    assert values
    assert all(0 <= int(value) <= 100 for value in values)
    assert abs(int(total.group(1)) - sum(map(int, values))) <= len(values) / 2
    assert "tatsächliche Kickbase-Punkte werden noch nicht prognostiziert" in html
    assert "% xP" not in html


def test_openligadb_outage_is_a_note_not_an_error(logged_in_client, fake_oldb):
    fake_oldb.down = True
    response = logged_in_client.get("/empfehlungen/kaufen")
    assert response.status_code == 200
    assert "OpenLigaDB nicht erreichbar" in response.text


def test_kickbase_outage_shows_message(logged_in_client, fake_kickbase):
    import httpx

    fake_kickbase.on("GET", "/v4/leagues/1000002/market", httpx.Response(503))
    html = logged_in_client.get("/empfehlungen/kaufen").text
    assert "Kickbase ist gerade nicht erreichbar" in html


def test_unknown_view_is_rejected(logged_in_client):
    assert logged_in_client.get("/empfehlungen/tausch").status_code == 422


def test_seller_names_can_be_blurred_in_screenshots(logged_in_client):
    assert 'class="rec__seller"' in logged_in_client.get("/empfehlungen/kaufen").text


def test_empty_states(logged_in_client, fake_kickbase):
    from tests.fakes import fixture

    fake_kickbase.on("GET", "/v4/leagues/1000002/market", {"it": []})
    # The squad first: the buy page loads (and caches) it too, for the squad gaps.
    squad = fixture("league_squad")
    for player in squad["it"]:
        player.update(st=0, sdmvt=0, tfhmvt=0)
    fake_kickbase.on("GET", "/v4/leagues/1000002/squad", squad)
    assert "Nichts Passendes auf dem Markt" in logged_in_client.get("/empfehlungen/kaufen").text
    assert "Keine Verkaufskandidaten" in logged_in_client.get("/empfehlungen/verkaufen").text
    html = logged_in_client.get("/empfehlungen/elf").text
    assert "Die belegten Plätze entsprechen bereits der Empfehlung" in html
    assert "Für eine volle Elf fehlen noch Spieler" in html


def test_clamped_score_is_labelled(logged_in_client, fake_kickbase):
    from tests.fakes import fixture

    squad = fixture("league_squad")
    squad["it"][0].update(st=1, sdmvt=-2_000_000)  # injured and falling: below 0
    fake_kickbase.on("GET", "/v4/leagues/1000002/squad", squad)
    assert "(auf 0–100 begrenzt)" in logged_in_client.get("/empfehlungen/verkaufen").text


def test_shown_parts_match_shown_scores_on_the_page(logged_in_client):
    import re

    html = logged_in_client.get("/empfehlungen/verkaufen").text
    for block in html.split('<li class="rec">')[1:]:
        total = int(re.search(r'rec__total">(\d+)<', block).group(1))
        parts = re.findall(
            r'score-parts__item[^"]*">\s*<span>[^<]+</span> <span class="num">([^<]+)<', block
        )
        values = [int(p.replace("−", "-").replace("+", "")) for p in parts]
        if "begrenzt" not in block:
            assert sum(values) == total


def test_eleven_page_marks_stand_ins(settings, logged_in_client):
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.models import Injury

    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(
            Injury(
                source="ligainsider",
                team_id="43",
                player_id="8227",
                player_name="Vandevoordt",
                kind="aufbautraining",
                reason="Muskelverletzung",
                since=None,
                fetched_at=datetime.now(UTC),
            )
        )
    engine.dispose()
    html = logged_in_client.get("/empfehlungen/elf").text
    assert "Notbesetzung" in html
    assert "Startchance 20 % (im Aufbautraining: Muskelverletzung" in html


def test_sell_page_minus_means_the_real_budget(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID, fixture

    path = f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget"
    budget = fixture("league_budget")
    budget.update(b=2_000_000, pbaa=-3_000_000)  # positive, but negative if all bids win
    fake_kickbase.on("GET", path, budget)
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert "Gewinnst du alle offenen Gebote, bist du" in html
    assert "Ins Plus kommen" not in html


def test_sell_page_guide_when_really_in_minus(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID, fixture

    budget = fixture("league_budget")
    budget.update(b=-5_000_000, pbaa=-5_000_000)
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert "Ins Plus kommen" in html
    assert 'Du bist <strong class="num">5,00 Mio.</strong> im Minus' in html
    assert "−5,00" not in html.split("Ins Plus kommen")[1].split("</p>")[0]


def test_sell_guide_adds_what_open_bids_would_cost(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID, fixture

    budget = fixture("league_budget")
    budget.update(b=-1_000_000, pbaa=-4_000_000)
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert 'Du bist <strong class="num">1,00 Mio.</strong> im Minus' in html
    assert "fehlen insgesamt" in html and "4,00 Mio." in html


def test_sell_reason_names_the_kickbase_status(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID, fixture

    squad = fixture("league_squad")
    squad["it"][0]["st"] = 128  # not in the league any more
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert "verletzt/gesperrt" not in html
    from app.kickbase.models import status_label

    assert f"Startchance 0 %: {status_label(128)}" in html


def test_sell_and_eleven_cards_are_not_foldable(logged_in_client):
    # The compact, foldable row (with its chevron) is only for the buy list.
    for view in ("verkaufen", "elf"):
        html = logged_in_client.get(f"/empfehlungen/{view}").text
        assert "rec__row" not in html and "rec__fold" not in html


def test_weights_note_lists_every_component(logged_in_client):
    html = logged_in_client.get("/empfehlungen/verkaufen").text
    assert "Klasse 0,30" in html and "Trend 0,25" in html
