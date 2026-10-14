"""Tests for the watchlist web UI (Block C, C1.2).

Pattern follows ``tests/test_player_pages.py``: ``logged_in_client`` for the cookie, the
``htmx_csrf_headers`` helper to read the per-session CSRF token from the layout and to set the
``HX-Request`` header. Players are seeded with the same ``seed_players`` helper so the service
layer accepts them (it refuses unknown IDs)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Watchlist
from tests.test_player_pages import (
    PID,
    add_player_endpoints,
    htmx_csrf_headers,
    seed_players,
)


def _watched(settings, league_id: str = "1000002") -> list[str]:
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        rows = session.query(Watchlist).filter_by(league_id=league_id).all()
        ids = [r.player_id for r in rows]
    engine.dispose()
    return ids


def test_watchlist_page_is_empty_by_default(logged_in_client):
    html = logged_in_client.get("/merkliste").text
    assert "Merkliste" in html
    assert "watchlist__empty" in html
    assert "Die Merkliste ist leer." in html
    assert 'class="watchlist-star"' not in html  # nothing to toggle in the list


def test_watchlist_add_swaps_the_star_to_watched(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    response = logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)
    assert response.status_code == 200
    assert "watchlist-star--on" in response.text
    assert 'aria-pressed="true"' in response.text
    assert 'aria-label="Auf der Merkliste"' in response.text
    assert _watched(settings) == [PID]


def test_watchlist_remove_swaps_the_star_back(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)
    assert _watched(settings) == [PID]

    response = logged_in_client.post("/merkliste/remove", data={"player_id": PID}, headers=headers)
    assert response.status_code == 200
    assert "watchlist-star--on" not in response.text
    assert 'aria-pressed="false"' in response.text
    assert _watched(settings) == []


def test_watchlist_remove_swaps_the_whole_list(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)

    response = logged_in_client.post(
        "/merkliste/remove",
        data={"player_id": PID},
        headers=headers | {"HX-Target": "watchlist-list"},
    )
    assert response.status_code == 200
    assert 'id="watchlist-list"' in response.text
    assert "watchlist__empty" in response.text
    # Follow-up GET shows the empty list (the same partial is rendered again on /merkliste).
    html = logged_in_client.get("/merkliste").text
    assert "watchlist__empty" in html
    assert "Hansen" not in html


def test_failed_watchlist_remove_preserves_star_and_entry(settings, logged_in_client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    from app.services import watchlist

    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)

    async def fail(*args):
        raise SQLAlchemyError("fixture database failure")

    monkeypatch.setattr(watchlist, "remove", fail)
    response = logged_in_client.post("/merkliste/remove", data={"player_id": PID}, headers=headers)
    assert 'aria-pressed="true"' in response.text
    assert 'role="alert"' in response.text
    assert _watched(settings) == [PID]
    response = logged_in_client.post(
        "/merkliste/remove",
        data={"player_id": PID},
        headers=headers | {"HX-Target": "watchlist-list"},
    )
    assert response.status_code == 503
    assert _watched(settings) == [PID]


def test_watchlist_page_lists_entries_with_value_and_status(
    settings, logged_in_client, fake_kickbase
):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)

    html = logged_in_client.get("/merkliste").text
    assert f'href="/spieler/{PID}">Hansen</a>' in html
    assert "watchlist__row" in html
    assert "3.7 Mio" in html or "3,7" in html  # market value of the seeded player
    assert "angeschlagen" in html  # status=2 → badge "angeschlagen"
    assert "nicht gelistet" in html  # the fixture market is empty


def test_watchlist_threshold_set_and_clear(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)

    response = logged_in_client.post(
        "/merkliste/threshold",
        data={"player_id": PID, "price_below": "1500000"},
        headers=headers,
    )
    assert response.status_code == 200
    assert "watchlist__threshold-edit" in response.text
    assert "watchlist__threshold" in response.text  # the "Schwelle: …" line
    assert "1.5 Mio" in response.text or "1,5" in response.text
    assert 'value="1500000"' in response.text  # echoed back into the input

    response = logged_in_client.post(
        "/merkliste/threshold",
        data={"player_id": PID, "price_below": ""},
        headers=headers,
    )
    assert response.status_code == 200
    # No "Schwelle: …" line when the threshold is cleared (the form stays).
    assert 'watchlist__threshold">' not in response.text
    assert 'value=""' in response.text  # the empty value stays in the input
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        row = session.get(Watchlist, ("1000002", PID))
        assert row is not None and row.price_below is None
    engine.dispose()


def test_watchlist_threshold_on_missing_entry_is_an_error(
    settings, logged_in_client, fake_kickbase
):
    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    response = logged_in_client.post(
        "/merkliste/threshold",
        data={"player_id": PID, "price_below": "1500000"},
        headers=headers,
    )
    assert response.status_code == 200
    assert "Spieler nicht mehr auf der Merkliste" in response.text


@pytest.mark.parametrize("failure", ["write", "reload"])
def test_threshold_db_failure_preserves_editable_row(
    settings, logged_in_client, monkeypatch, failure
):
    from sqlalchemy.exc import SQLAlchemyError

    from app.services import watchlist
    from app.web.routes import pages

    seed_players(settings)
    headers = htmx_csrf_headers(logged_in_client)
    logged_in_client.post("/merkliste/add", data={"player_id": PID}, headers=headers)

    async def fail(*args):
        raise SQLAlchemyError("fixture database failure")

    if failure == "write":
        monkeypatch.setattr(watchlist, "set_threshold", fail)
    else:
        monkeypatch.setattr(pages, "_watchlist_page_data", fail)
    response = logged_in_client.post(
        "/merkliste/threshold", data={"player_id": PID, "price_below": "1234567"}, headers=headers
    )
    assert response.status_code == 503  # HTMX preserves the DOM on an error response.
    assert "Spieler nicht mehr" not in response.text
    assert _watched(settings) == [PID]
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        assert session.get(Watchlist, ("1000002", PID)).price_below == (
            1234567 if failure == "reload" else None
        )
    engine.dispose()


def test_watchlist_endpoints_require_csrf(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    headers_no_csrf = {"HX-Request": "true"}
    assert (
        logged_in_client.post(
            "/merkliste/add", data={"player_id": PID}, headers=headers_no_csrf
        ).status_code
        == 403
    )
    assert (
        logged_in_client.post(
            "/merkliste/remove", data={"player_id": PID}, headers=headers_no_csrf
        ).status_code
        == 403
    )
    assert (
        logged_in_client.post(
            "/merkliste/threshold",
            data={"player_id": PID, "price_below": "1500000"},
            headers=headers_no_csrf,
        ).status_code
        == 403
    )
    # Plain POST without HTMX header is rejected as well.
    assert logged_in_client.post("/merkliste/add", data={"player_id": PID}).status_code == 403


def test_player_page_shows_the_star_and_link_to_the_watchlist(logged_in_client, fake_kickbase):
    add_player_endpoints(fake_kickbase)
    html = logged_in_client.get(f"/spieler/{PID}").text
    assert 'class="watchlist-star"' in html
    assert "Merkliste ansehen" in html
    assert 'href="/merkliste"' in html


def test_players_list_shows_a_star_per_row(settings, logged_in_client, fake_kickbase):
    seed_players(settings)
    html = logged_in_client.get("/spieler?q=han").text
    assert "player-row__aside" in html
    assert 'class="watchlist-star"' in html
    assert f'"player_id": "{PID}"' in html
    assert 'hx-post="/merkliste/add"' in html
