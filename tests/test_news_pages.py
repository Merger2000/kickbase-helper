from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import KeyValue, NewsItem, NewsReport
from app.news.service import FEED_STATUS_KEY
from tests.test_player_pages import PID, add_player_endpoints

# PID (Hansen, Paderborn) is also in the test squad.


def seed_news(settings, *, absence="rund drei Wochen", age=timedelta(days=1), feed_ok=True):
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(
            NewsItem(
                guid="g1",
                source="kicker",
                title="Paderborn bangt um Hansen",
                teaser="",
                link="https://www.kicker.de/hansen-1/artikel",
                categories=[],
                published_at=now - age,
                fetched_at=now,
                processed_at=now,
            )
        )
        session.add(
            NewsReport(
                news_guid="g1",
                published_at=now - age,
                player_name="Hansen",
                team_id="29",
                player_id=PID,
                kind="verletzung",
                status="faellt_aus",
                absence=absence,
                expected_return="nach der Länderspielpause",
            )
        )
        session.add(
            KeyValue(
                key=FEED_STATUS_KEY,
                value={"ok": feed_ok, "at": now.isoformat(), "detail": "HTTP 500"},
                updated_at=now,
            )
        )
    engine.dispose()


def test_team_page_lists_squad_news(settings, logged_in_client):
    seed_news(settings)
    html = logged_in_client.get("/").text
    assert "Kader-Nachrichten" in html
    assert f'<a href="/spieler/{PID}">Hansen</a>' in html
    assert '<span class="badge badge--out">fällt aus</span>' in html
    assert "Verletzung: rund drei Wochen, Rückkehr: nach der Länderspielpause" in html
    assert (
        '<a class="news__source" href="https://www.kicker.de/hansen-1/artikel" '
        'target="_blank" rel="noopener noreferrer">Paderborn bangt um Hansen' in html
    )


def test_team_page_hides_old_news_but_shows_feed_problem(settings, logged_in_client):
    seed_news(settings, age=timedelta(days=10), feed_ok=False)
    html = logged_in_client.get("/").text
    assert "Paderborn bangt um Hansen" not in html
    assert "kicker-Meldungen zuletzt nicht lesbar (HTTP 500)." in html


def test_team_page_before_first_news_run(logged_in_client):
    html = logged_in_client.get("/").text
    assert "Noch keine kicker-Meldungen gesammelt." in html


def test_player_page_lists_news_escaped(settings, logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    seed_news(settings, absence="<script>alert(1)</script>")
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert '<h2 id="news-title">Originalartikel &amp; Meldungen</h2>' in html
    assert "Paderborn bangt um Hansen" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_player_page_without_news(settings, logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    seed_news(settings)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.query(NewsReport).delete()
    engine.dispose()
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert "Keine Meldungen in den letzten 30 Tagen." in html


def set_kv(settings, key, value):
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.merge(KeyValue(key=key, value=value, updated_at=datetime.now(UTC)))
    engine.dispose()


def test_player_page_without_ai_says_so_instead_of_no_news(
    settings, logged_in_client, fake_kickbase
):
    from app.news.service import EXTRACT_STATUS_KEY

    add_player_endpoints(fake_kickbase)
    seed_news(settings)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.query(NewsReport).delete()
    engine.dispose()
    set_kv(settings, EXTRACT_STATUS_KEY, {"at": datetime.now(UTC).isoformat(), "state": "off"})
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert "KI-Ausfallanalyse aus; Originalartikel bleiben verfügbar." in html
    assert "Keine Meldungen in den letzten 30 Tagen." not in html


def test_team_page_shows_newest_report_per_player(settings, logged_in_client):
    seed_news(settings, age=timedelta(days=2))
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(
            NewsItem(
                guid="g2",
                source="kicker",
                title="Hansen wieder fit",
                teaser="",
                link="https://www.kicker.de/hansen-2/artikel",
                categories=[],
                published_at=now - timedelta(hours=2),
                fetched_at=now,
                processed_at=now,
            )
        )
        session.add(
            NewsReport(
                news_guid="g2",
                published_at=now - timedelta(hours=2),
                player_name="Hansen",
                team_id="29",
                player_id=PID,
                kind="rueckkehr",
                status="einsatzbereit",
                absence=None,
                expected_return=None,
            )
        )
    engine.dispose()
    html = logged_in_client.get("/").text
    assert "Hansen wieder fit" in html
    assert "Paderborn bangt um Hansen" not in html
    assert '<span class="badge badge--fit">wieder einsatzbereit</span>' in html


def test_database_error_only_hides_the_news(logged_in_client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import app.web.routes.pages as pages

    async def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("db down"))

    monkeypatch.setattr(pages, "reports_for_players", broken)
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert 'id="squad-title"' in response.text  # the team itself is shown
    assert "Die Datenbank ist gerade nicht erreichbar." in response.text


def test_original_article_is_visible_without_ai_and_old_report_is_archived(
    settings, logged_in_client, fake_kickbase
):
    from app.news.service import EXTRACT_STATUS_KEY
    from tests.test_player_pages import seed_players

    add_player_endpoints(fake_kickbase)
    seed_players(settings)
    seed_news(settings, age=timedelta(days=5))
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add(
            NewsItem(
                guid="original-only",
                source="kicker",
                title="Hansen: Die neue Rolle im Training",
                teaser="Kein bestätigter Ausfall",
                link="https://www.kicker.de/hansen-3/artikel",
                categories=["Hansen", "SC Paderborn 07"],
                published_at=datetime.now(UTC) - timedelta(hours=3),
                fetched_at=datetime.now(UTC),
            )
        )
    engine.dispose()
    set_kv(settings, EXTRACT_STATUS_KEY, {"at": datetime.now(UTC).isoformat(), "state": "off"})
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert "Hansen: Die neue Rolle im Training" in html
    assert "Originalartikel · kicker" in html
    assert "Meldungsarchiv · letzte 30 Tage (1)" in html
    assert "Meldung vom" in html
    assert html.count("Paderborn bangt um Hansen") == 1  # no duplicate article/report


def test_team_page_orders_article_revisions_by_observed_change(settings, logged_in_client):
    seed_news(settings, age=timedelta(days=20))
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        article = session.get(NewsItem, "g1")
        article.title = "Hansen: aktualisierte Meldung"
        article.updated_at = now - timedelta(minutes=10)
        article.revision = 2
        report = session.query(NewsReport).one()
        report.revision = 2
        report.source_excerpt = "Trainingsstand: <script>privater Text</script>"
        session.add(
            NewsItem(
                guid="g2",
                source="kicker",
                title="Torwart: neue Meldung",
                teaser="",
                link="https://www.kicker.de/torwart-2/artikel",
                categories=[],
                published_at=now - timedelta(hours=1),
                fetched_at=now,
                processed_at=now,
            )
        )
        session.add(
            NewsReport(
                news_guid="g2",
                published_at=now - timedelta(hours=1),
                player_name="Vandevoordt",
                team_id="43",
                player_id="8227",
                kind="verletzung",
                status="faellt_aus",
            )
        )
    engine.dispose()
    html = logged_in_client.get("/").text
    assert html.index("Hansen: aktualisierte Meldung") < html.index("Torwart: neue Meldung")
    assert "Artikeländerung erkannt" in html and "Revision 2" in html
    assert "Meldungsarchiv" not in html  # the changed body is recent despite the original date
    assert "&lt;script&gt;privater Text&lt;/script&gt;" in html
    assert "<script>privater Text</script>" not in html
