from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db.models import MarketValue
from app.kickbase.calendar import market_day
from app.kickbase.client import Fetched
from app.kickbase.models import Squad
from app.services.daily_trends import build_trends, load_trends
from tests.fakes import TEAM_LEAGUE_ID, fixture
from tests.test_transfers import sessions as transfer_sessions

sessions = transfer_sessions
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
DAY = market_day(NOW)


def source(delta=-100, value=1000):
    raw = fixture("league_squad")["it"][0] | {"i": "1", "mv": value, "tfhmvt": delta}
    return Fetched(Squad.model_validate({"it": [raw]}), NOW)


def history(values):
    return {("1", DAY - timedelta(days=back)): value for back, value in enumerate(reversed(values))}


def test_turn_from_rising_to_falling_is_confirmed_by_api_and_exact_history():
    trend = build_trends(source(), history([1000, 1100, 1000]), NOW).players[0]
    assert trend.confirmed and trend.turn == "down"
    assert trend.latest == -100 and trend.streak == 1
    assert "nach unten" in trend.label


def test_rising_streak_and_turn_in_other_direction():
    assert build_trends(source(100, 1200), history([1000, 1100, 1200]), NOW).players[0].streak == 2
    assert build_trends(source(100, 1000), history([1000, 900, 1000]), NOW).players[0].turn == "up"


@pytest.mark.parametrize(
    "fault",
    [
        "value",
        "delta",
        "missing_daily",
        "stale",
        "old_cache",
        "old_same_day",
        "future_cache",
        "missing_today",
    ],
)
def test_current_mismatch_or_incomplete_data_does_not_confirm_turn(fault):
    cached = source()
    values = history([1000, 1100, 1000])
    if fault == "value":
        cached = source(value=999)
    elif fault == "delta":
        cached = source(delta=-99)
    elif fault == "missing_daily":
        raw = fixture("league_squad")["it"][0] | {"i": "1", "mv": 1000}
        raw.pop("tfhmvt", None)
        cached = Fetched(Squad.model_validate({"it": [raw]}), NOW)
    elif fault == "stale":
        cached = replace(cached, stale=True)
    elif fault == "old_cache":
        cached = replace(cached, fetched_at=NOW - timedelta(days=1))
    elif fault == "old_same_day":
        cached = replace(cached, fetched_at=NOW - timedelta(hours=1))
    elif fault == "future_cache":
        cached = replace(cached, fetched_at=NOW + timedelta(minutes=1))
    elif fault == "missing_today":
        values.pop(("1", DAY))
    trend = build_trends(cached, values, NOW).players[0]
    assert not trend.confirmed and trend.turn is None and trend.streak == 0


def test_zero_is_confirmed_and_breaks_streak_without_inventing_turn():
    trend = build_trends(source(0, 1000), history([900, 1000, 1000]), NOW).players[0]
    assert trend.confirmed and trend.latest == 0 and trend.turn is None and trend.streak == 0
    trend = build_trends(source(100, 1100), history([900, 1000, 1000, 1100]), NOW).players[0]
    assert trend.confirmed and trend.turn is None and trend.streak == 1


def test_gaps_are_not_multiple_days_disguised_as_daily_movement():
    values = history([1000, 1100, 1000])
    values.pop(("1", DAY - timedelta(days=1)))
    trend = build_trends(source(), values, NOW).players[0]
    assert trend.latest is None and not trend.confirmed
    values = history([900, 1000, 1100])
    values.pop(("1", DAY - timedelta(days=2)))
    trend = build_trends(source(100, 1100), values, NOW).players[0]
    assert trend.confirmed and trend.streak == 1 and trend.turn is None


@pytest.mark.parametrize("guard", ["now", "cache"])
def test_market_update_window_suppresses_confirmation(guard):
    current = NOW.replace(hour=20, minute=20)
    fetched = source()
    if guard == "cache":
        current = NOW.replace(hour=21)
        fetched = replace(fetched, fetched_at=NOW.replace(hour=20, minute=20))
    else:
        fetched = replace(fetched, fetched_at=current)
    day = market_day(current)
    values = {
        ("1", day): 1000,
        ("1", day - timedelta(days=1)): 1100,
        ("1", day - timedelta(days=2)): 1000,
    }
    assert not build_trends(fetched, values, current).players[0].confirmed


async def test_database_load_ignores_future_and_other_players_and_is_read_only(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                MarketValue(player_id=pid, day=day, value=value)
                for (pid, day), value in history([1000, 1100, 1000]).items()
            ]
        )
        session.add(MarketValue(player_id="1", day=DAY + timedelta(days=1), value=9))
        session.add(MarketValue(player_id="other", day=DAY, value=99))
    data = await load_trends(sessions, source(), NOW)
    assert data.turns[0].turn == "down" and not data.others
    assert (await load_trends(sessions, source(), NOW)) == data
    async with sessions() as session:
        assert len((await session.scalars(select(MarketValue))).all()) == 5


def test_page_does_not_fetch_market_value_history(logged_in_client, fake_kickbase):
    response = logged_in_client.get("/empfehlungen/trends")
    assert response.status_code == 200 and "Sieben gespeicherte Tagesänderungen" in response.text
    assert "Keine bestätigte Trendwende" in response.text
    assert "noch nicht bestätigt" in response.text
    assert not any(
        "/marketvalue" in r.url.path or "activitiesFeed" in r.url.path
        for r in fake_kickbase.requests
    )


def test_outage_is_visible(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
    assert (
        "Kickbase ist gerade nicht erreichbar" in logged_in_client.get("/empfehlungen/trends").text
    )
