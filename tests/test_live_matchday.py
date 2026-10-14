from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.kickbase import models as m
from app.kickbase.client import Fetched, cache_key
from app.kickbase.league import ResolvedLeague
from app.kickbase.stores import CacheEntry, MemoryResponseCache
from app.services.live_matchday import build_live_matchday, load_live_matchday, started_matchday
from tests.fakes import FakeClock, FakeKickbase, make_client

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
UID = "9000001"
PATH = "/v4/leagues/l/users/9000001/teamcenter"


def schedule():
    return m.Matchdays.model_validate(
        {
            "day": 7,
            "it": [
                {
                    "day": 5,
                    "it": [
                        {
                            "mi": "1",
                            "day": 5,
                            "dt": (NOW - timedelta(days=7)).isoformat(),
                            "t1": "1",
                            "t2": "2",
                            "st": 2,
                        }
                    ],
                },
                {
                    "day": 6,
                    "it": [
                        {
                            "mi": "2",
                            "day": 6,
                            "dt": (NOW - timedelta(minutes=30)).isoformat(),
                            "t1": "1",
                            "t2": "2",
                            "st": None,
                        }
                    ],
                },
                {
                    "day": 7,
                    "it": [
                        {
                            "mi": "3",
                            "day": 7,
                            "dt": (NOW + timedelta(days=7)).isoformat(),
                            "t1": "1",
                            "t2": "2",
                            "st": 0,
                        }
                    ],
                },
            ],
        }
    )


def center():
    return {
        "n": "Own manager",
        "lp": [
            {"i": "1", "n": "Zero", "tid": "1", "p": 0, "mst": 2},
            {"i": "2", "n": "Minus", "tid": "1", "p": -12, "mst": 1},
            {"i": "3", "n": "Missing", "tid": "2", "mst": None},
        ],
        "nlp": [{"i": "4", "n": "Bank", "tid": "2", "p": 999, "mst": 2}],
        "us": [
            {"i": "other", "unm": "Never shown", "mdp": 99999},
            {"i": UID, "unm": "Own", "mdp": -10},
        ],
    }


def build(payload=None):
    s = schedule()
    day = started_matchday(s, NOW)
    return build_live_matchday(
        Fetched(m.UserTeamCenter.model_validate(payload or center()), NOW),
        Fetched(s, NOW),
        day,
        UID,
        NOW,
    )


def test_latest_started_round_ignores_next_day_and_postponed_older_game():
    s = schedule()
    assert started_matchday(s, NOW).matchday == 6
    s.matchdays[0].matches.append(
        m.Match.model_validate({"mi": "4", "day": 5, "dt": NOW.isoformat(), "t1": "3", "t2": "4"})
    )
    assert started_matchday(s, NOW).matchday == 6
    assert started_matchday(s, NOW - timedelta(days=20)) is None


def test_missing_zero_negative_and_own_manager_not_first():
    view = build()
    assert [p.points for p in view.lineup] == [0, -12, None]
    assert view.official_points == -10
    assert view.lineup_points is None
    assert view.open_slots == 8
    assert view.auto_refresh
    assert view.lineup[1].status_label == "Status offen"  # mst=1 has no verified meaning
    assert view.lineup[0].status_label == "Abgepfiffen"


def test_bank_is_excluded_and_official_sum_is_authoritative():
    payload = center()
    payload["lp"][2]["p"] = 5
    view = build(payload)
    assert view.lineup_points == -7
    assert view.official_points == -10
    assert view.differs
    assert view.bench[0].points == 999


@pytest.mark.parametrize(
    "users", [[], [{"i": "other", "unm": "Never shown", "mdp": 99999}], [{"i": UID, "unm": "Own"}]]
)
def test_missing_own_summary_is_unknown(users):
    payload = center()
    payload["us"] = users
    assert build(payload).official_points is None


def test_stale_sources_and_no_polling_after_game_window():
    s = schedule()
    day = started_matchday(s, NOW)
    view = build_live_matchday(
        Fetched(m.UserTeamCenter.model_validate(center()), NOW),
        Fetched(s, NOW, True),
        day,
        UID,
        NOW + timedelta(hours=7),
    )
    assert view.stale
    assert not view.auto_refresh


async def test_live_cache_replaces_old_24h_entry_and_respects_10min():
    fake = FakeKickbase()
    clock = FakeClock(NOW)
    cache = MemoryResponseCache()
    old = NOW - timedelta(hours=2)
    key = cache_key(PATH, {"dayNumber": 6})
    await cache.set(key, CacheEntry(center(), old, NOW + timedelta(hours=22)))
    payload = center()
    payload["lp"][0]["p"] = 42
    fake.on("GET", PATH, payload)
    async with make_client(fake, clock=clock, cache=cache) as client:
        report = await client.user_teamcenter("l", UID, 6)
        assert report.data.lineup[0].points == 0
        assert fake.gets(PATH) == []
        result = await client.user_teamcenter("l", UID, 6, live=True)
        assert result.data.lineup[0].points == 42
        assert len(fake.gets(PATH)) == 1
        clock.advance(seconds=599)
        await client.user_teamcenter("l", UID, 6, live=True)
        assert len(fake.gets(PATH)) == 1
        clock.advance(seconds=1)
        await client.user_teamcenter("l", UID, 6, live=True)
        assert len(fake.gets(PATH)) == 2
        fake.on("GET", PATH, httpx.Response(503))
        clock.advance(minutes=10)
        stale = await client.user_teamcenter("l", UID, 6, live=True)
        assert stale.stale
        assert stale.fetched_at == NOW + timedelta(minutes=10)


async def test_loader_uses_frozen_lineup_without_current_squad():
    fake = FakeKickbase()
    clock = FakeClock(NOW)
    fake.on(
        "GET", "/v4/competitions/c/matchdays", schedule().model_dump(by_alias=True, mode="json")
    )
    fake.on("GET", "/v4/user/me", {"u": {"id": UID, "name": "Own"}})
    fake.on("GET", PATH, center())
    async with make_client(fake, clock=clock) as client:
        view = await load_live_matchday(client, ResolvedLeague("l", "Test", "c"), NOW)
        assert [p.id for p in view.lineup] == ["1", "2", "3"]
        assert not any("/squad" in r.url.path or "/lineup" in r.url.path for r in fake.requests)
        assert fake.gets(PATH)[0].url.params["dayNumber"] == "6"


def install_points(fake):
    from tests.fakes import TEAM_LEAGUE_ID

    # Relative times keep page tests independent from today's date.
    now = datetime.now(UTC)
    data = schedule().model_dump(by_alias=True, mode="json")
    for day in data["it"]:
        for match in day["it"]:
            match["dt"] = (now + timedelta(days=day["day"] - 6) - timedelta(minutes=30)).isoformat()
    fake.on("GET", "/v4/competitions/1/matchdays", data)
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/users/{UID}/teamcenter", center())


def test_page_and_partial_show_own_points_and_provisional_value(logged_in_client, fake_kickbase):
    install_points(fake_kickbase)
    page = logged_in_client.get("/spieltag")
    assert page.status_code == 200
    assert "6. Spieltag" in page.text
    assert "Wertung vorläufig" in page.text
    assert "Never shown" not in page.text
    assert "99999" not in page.text
    assert "unbekannt" in page.text
    assert "-12" in page.text
    partial = logged_in_client.get("/spieltag/punkte", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "<!doctype html>" not in partial.text.lower()
    assert 'data-auto-refresh="true"' in partial.text


def test_endpoint_denied_is_visible_without_breaking_team(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID

    install_points(fake_kickbase)
    fake_kickbase.on(
        "GET",
        f"/v4/leagues/{TEAM_LEAGUE_ID}/users/{UID}/teamcenter",
        httpx.Response(403, json={"err": 1}),
    )
    page = logged_in_client.get("/spieltag")
    assert page.status_code == 200
    assert "nicht" in page.text
    assert "Kickbase-Spieltagspunkte" not in page.text
    assert 'data-auto-refresh="false"' in page.text
    assert logged_in_client.get("/").status_code == 200


def test_points_require_normal_login(client):
    response = client.get("/spieltag/punkte", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


async def test_no_started_matchday_does_not_fetch_user_or_points():
    fake = FakeKickbase()
    fake.on(
        "GET", "/v4/competitions/c/matchdays", schedule().model_dump(by_alias=True, mode="json")
    )
    async with make_client(fake, clock=FakeClock(NOW)) as client:
        assert (
            await load_live_matchday(
                client, ResolvedLeague("l", "Test", "c"), NOW - timedelta(days=20)
            )
            is None
        )
    assert [r.url.path for r in fake.gets()] == ["/v4/competitions/c/matchdays"]


def test_zero_official_total_is_not_missing():
    payload = center()
    payload["us"][1]["mdp"] = 0
    assert build(payload).official_points == 0


def test_temporary_database_failure_keeps_bounded_retries(logged_in_client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import app.web.routes.pages as pages

    async def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("temporarily down"))

    monkeypatch.setattr(pages, "load_live_matchday", broken)
    response = logged_in_client.get("/spieltag/punkte")
    assert response.status_code == 200
    assert "Die Datenbank ist gerade nicht erreichbar" in response.text
    assert 'data-auto-refresh="true"' in response.text
    assert 'data-interval="600"' in response.text


def test_match_length_is_not_shown_as_player_minutes(logged_in_client, fake_kickbase):
    install_points(fake_kickbase)
    payload = center()
    payload["lp"][0]["mt"] = 95  # same match-length field on substituted and full-game players
    from tests.fakes import TEAM_LEAGUE_ID

    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/users/{UID}/teamcenter", payload)
    response = logged_in_client.get("/spieltag")
    assert "95 Min." not in response.text
    assert "Abgepfiffen" in response.text
