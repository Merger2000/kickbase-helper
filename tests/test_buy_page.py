from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import LeagueTransfer
from tests.fakes import TEAM_LEAGUE_ID, fixture

MARKET_PATH = f"/v4/leagues/{TEAM_LEAGUE_ID}/market"


def market(deadline_in: timedelta = timedelta(days=2)) -> dict:
    """The market fixture with the deadline relative to now; the first Kickbase listing ends
    after the deadline, all others before."""
    data = fixture("league_market")
    data["dt"] = (datetime.now(UTC) + deadline_in).isoformat().replace("+00:00", "Z")
    late = next(p for p in data["it"] if not p.get("u"))
    late["exs"] = int((deadline_in + timedelta(days=1)).total_seconds())
    for p in data["it"]:
        if p is not late and p.get("exs"):
            p["exs"] = 3600
    return data


def seed_transfers(settings, count=12, premium=0.1):
    engine = create_engine(settings.database_url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        for i in range(count):
            value = 2_000_000 + i * 1_000_000
            session.add(
                LeagueTransfer(
                    activity_id=str(i),
                    league_id=TEAM_LEAGUE_ID,
                    at=now - timedelta(days=1),
                    player_id=str(i),
                    player_name="X",
                    team_id=None,
                    kind=1,
                    price=round(value * (1 + premium)),
                    by_me=False,
                    market_value=value,
                )
            )
    engine.dispose()


def seed_job_run(settings, hours_ago: float, replace: bool = False):
    from app.db.models import JobRun

    engine = create_engine(settings.database_url)
    at = datetime.now(UTC) - timedelta(hours=hours_ago)
    with Session(engine) as session, session.begin():
        if replace:
            session.query(JobRun).delete()
        session.add(
            JobRun(job="collect_league_transfers", started_at=at, finished_at=at, status="ok")
        )
    engine.dispose()


def test_unknown_or_past_deadline_hides_the_filter(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market(deadline_in=-timedelta(hours=1)))
    html = logged_in_client.get("/empfehlungen/kaufen").text
    assert "Aufstellungs-Deadline ist gerade nicht bekannt" in html
    assert 'name="vor"' not in html
    assert 'class="rec__timing"' not in html


def test_stale_gaps_filter_is_ignored(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    squad = fixture("league_squad")
    for p in squad["it"]:
        p["st"] = 0
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    html = logged_in_client.get("/empfehlungen/kaufen?luecke=1&vor=0").text
    if 'name="luecke"' not in html:  # no gaps: the filter must not hide everything
        assert 'class="rec"' in html


def test_htmx_error_renders_only_the_results_box(logged_in_client, fake_kickbase):
    import httpx

    fake_kickbase.on("GET", MARKET_PATH, httpx.Response(500))
    response = logged_in_client.get("/empfehlungen/kaufen", headers={"HX-Request": "true"})
    assert response.text.lstrip().startswith('<div class="buy-results" id="buy-results">')
    assert 'class="alert"' in response.text and "<nav" not in response.text


def test_history_restore_gets_the_whole_page(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    headers = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
    assert "<html" in logged_in_client.get("/empfehlungen/kaufen", headers=headers).text


def test_buy_page_shows_deadline_filters_and_hides_late_offers(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen").text
    assert "Nächste Aufstellungs-Deadline" in html
    assert 'name="sort"' in html and 'name="max"' in html
    assert 'class="rec__timing">vor Deadline' in html
    assert 'badge--doubtful">erst nach der Deadline' not in html  # hidden by the default filter
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert 'badge--doubtful">erst nach der Deadline' in html


def test_hidden_field_and_checkbox_mean_checked(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0&vor=1").text
    assert 'badge--doubtful">erst nach der Deadline' not in html


def test_bid_recommendation_needs_transfers(settings, logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert "Die Liga-Transfers wurden noch nicht gesammelt." in html
    assert "Gebotsempfehlung ab 10 Käufen mit Marktwert; bisher 0." in html
    assert 'class="bid"' not in html
    seed_transfers(settings)
    seed_job_run(settings, hours_ago=1)
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert 'class="bid"' in html
    assert "+10,0 %" in html  # every comparable purchase paid 10 % more
    assert "in den letzten 60 Tagen" in html
    assert "Höher als 70 % der Aufschläge auf den Marktwert, die bisher bezahlt wurden." in html
    assert "noch nicht gesammelt" not in html and "Gebotsempfehlung ab" not in html
    seed_job_run(settings, hours_ago=8, replace=True)
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert "seit über 6 Stunden nicht aktualisiert" in html


def metas(html: str) -> list[str]:
    return [part.split("</span>")[0] for part in html.split('class="rec__meta">')[1:]]


def test_htmx_filter_returns_only_results(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    all_labels = [
        meta[:3].rstrip(",")
        for meta in metas(logged_in_client.get("/empfehlungen/kaufen?vor=0").text)
    ]
    label = all_labels[0]
    pos = {"TW": 1, "ABW": 2, "MF": 3, "ST": 4}[label]
    response = logged_in_client.get(
        f"/empfehlungen/kaufen?vor=0&pos={pos}&sort=preis", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="buy-results" id="buy-results">')
    assert "<html" not in response.text
    filtered = metas(response.text)
    assert filtered and all(meta.startswith(label) for meta in filtered)
    assert len(filtered) == all_labels.count(label)


def test_max_price_in_millions(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0&max=0,5").text
    assert "Kein Angebot passt zu den Filtern" in html or 'class="rec"' in html
    assert 'value="0,5"' in html


def test_over_budget_toggle_shows_over_budget_offers(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert 'name="ueber"' in html  # toggle is offered when there are over-budget offers
    assert "über dem Budget ausgeblendet" in html
    shown = logged_in_client.get("/empfehlungen/kaufen?vor=0&ueber=1").text
    assert 'name="ueber" value="1" checked' in shown
    assert "über dem Budget ausgeblendet" not in shown
    assert 'badge--doubtful">über Budget' in shown  # over-budget offers are marked


def test_over_budget_toggle_hidden_when_everything_is_affordable(logged_in_client, fake_kickbase):
    budget = fixture("league_budget")
    budget["pbaa"] = 100_000_000.0
    budget["b"] = 100_000_000.0
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen?vor=0").text
    assert 'name="ueber"' not in html


def test_invalid_filters_are_rejected(logged_in_client):
    for query in ("sort=x", "pos=7", "vor=2", "ueber=2", "max=abc", "max=10000"):
        assert logged_in_client.get(f"/empfehlungen/kaufen?{query}").status_code == 422


def test_squad_gaps_are_shown(logged_in_client, fake_kickbase):
    squad = fixture("league_squad")
    for p in squad["it"]:
        if p["pos"] == 4:
            p["st"] = 1  # all forwards injured
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen").text
    assert "Für eine volle Elf fehlen" in html and "Sturm" in html
    assert 'name="luecke"' in html


def test_buy_row_is_compact_and_marks_own_bids(logged_in_client, fake_kickbase):
    from bs4 import BeautifulSoup

    # Deadline relative to now: the fixed fixture deadline made this fail shortly before it.
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen").text
    # One summary row per offer, details folded away with a link to the player page.
    assert html.count('<summary class="rec__row">') == html.count('class="rec"')
    assert "Spielerseite öffnen" in html
    assert "dein Gebot 500" in html  # the fixture bids 500.500 € on Røssing-Lelesiit
    rows = BeautifulSoup(html, "html.parser").select("summary.rec__row")
    assert any(row.select_one(".rec__data-note") for row in rows)  # visible before opening


def test_data_notes_are_bundled(logged_in_client):
    html = logged_in_client.get("/empfehlungen/kaufen").text
    # Several hints (no points yet, incomplete matchdays, bid data) fold into one box …
    assert html.count('class="note data-notes"') == 1
    assert "Hinweise zu den Daten</summary>" in html
    assert "Gebotsempfehlung ab 10 Käufen" in html  # … and stay readable when opened


def test_single_data_note_stays_visible(app):
    from app.web.templating import templates

    macros = templates.env.get_template("macros.html").module
    assert str(macros.data_notes(["Nur einer", None])).strip() == '<p class="note">Nur einer</p>'
    assert str(macros.data_notes([])).strip() == ""


def test_source_failures_stay_visible_next_to_folded_notes(logged_in_client, fake_oldb):
    fake_oldb.down = True
    html = logged_in_client.get("/empfehlungen/kaufen").text
    warning = html.index('class="note note--warning">Programm nicht verfügbar')
    folded = html.index('class="note data-notes"')
    assert warning < folded  # shown above, not inside the folded box


def test_data_notes_macro_with_warnings(app):
    from app.web.templating import templates

    macros = templates.env.get_template("macros.html").module
    html = str(macros.data_notes(["a", "b"], ["Quelle kaputt"]))
    assert html.index("Quelle kaputt") < html.index("<details")
    assert "2 Hinweise zu den Daten" in html


# ---------------------------------------------------------------- C4.3 xP/Mio.


def test_buy_page_lists_xp_pro_mio_sort_and_breakdown(logged_in_client, fake_kickbase):
    """C4.3: Kaufseite zeigt xP-Spalte, xP-Sortierung und xP-Aufschlüsselung in jeder Zeile."""
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get("/empfehlungen/kaufen").text
    # Sortierung wird im Filter-Dropdown angeboten …
    assert '<option value="xp-pro-mio">Index pro Mio.</option>' in html
    # … und jede Kaufempfehlung trägt eine eigene xP-Aufschlüsselung.
    assert html.count("Wie entsteht der Aufstellungsindex?") >= 1
    assert "Index pro Mio." in html  # Spalte in den Fakten
    assert "Aufstellungsindex" in html
    assert "Erwartete Punkte" not in html


def test_buy_page_xp_pro_mio_sort_orders_by_xp_value_per_euro(logged_in_client, fake_kickbase):
    """Sort=xp-pro-mio rendert die Zeilen in absteigender xP/Mio.-Reihenfolge."""
    fake_kickbase.on("GET", MARKET_PATH, market())
    html = logged_in_client.get(
        "/empfehlungen/kaufen?vor=0&sort=xp-pro-mio", headers={"HX-Request": "true"}
    ).text
    # Hole die xP-pro-Mio-Werte aus jeder <dd> und prüfe die Reihenfolge.
    import re

    values = [
        float(m.replace(",", "."))
        for m in re.findall(r"Index pro Mio\.</dt><dd class=\"num\">([^<]+)</dd>", html)
    ]
    # Wenn die Regex nichts findet, ist die Sortierprüfung trivial – das muss
    # auffallen, sonst deckt der Test die eigentliche Anzeige nicht ab.
    assert values, "Regex hat keine xP-pro-Mio-Werte gefunden – Sortierung nicht prüfbar"
    assert values == sorted(values, reverse=True)
