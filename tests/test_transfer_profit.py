from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ApiCache, JobRun, LeagueTransfer
from app.kickbase import models as m
from app.kickbase.client import cache_key
from app.services.transfer_profit import TransferEvent, build_profit, load_profit
from app.services.transfers import backfill_sellers_from_cache, identify_sellers
from tests.fakes import NOW, TEAM_LEAGUE_ID, fixture
from tests.test_transfers import sessions as transfer_sessions

sessions = transfer_sessions


def buy(i="b", day=-4, price=1_100_000, player="1"):
    return TransferEvent(
        i, player, "Fixture Spieler", NOW + timedelta(days=day), 1, price, True, None
    )


def sale(i="s", day=-1, price=1_300_000, player="1"):
    return TransferEvent(
        i, player, "Fixture Spieler", NOW + timedelta(days=day), 2, price, False, True
    )


def squad(value=1_250_000):
    raw = fixture("league_squad")["it"][0]
    return [m.SquadPlayer.model_validate(raw | {"i": "1", "mv": value})]


def test_real_profit_counts_paid_premium_and_separate_open_position():
    events = [buy(), sale(), buy("b2", day=0, price=1_200_000)]
    data = build_profit(events, squad(), NOW)
    assert data.realized == 200_000 and data.open_gain == 50_000
    assert data.closed[0].held_days == 3
    assert data.closed[0].purchase_price == 1_100_000
    assert build_profit(events + events, squad(), NOW) == data  # replay does not double profits


def test_losses_and_confirmed_zero_are_real_results():
    assert build_profit([buy(), sale(price=1_000_000)], [], NOW).realized == -100_000
    assert build_profit([buy(), sale(price=1_100_000)], [], NOW).realized == 0
    assert build_profit([], squad(), NOW).realized is None
    assert build_profit([], squad(), NOW).open_gain is None  # no purchase ≠ zero gain


@pytest.mark.parametrize(
    "interruption",
    [
        replace(sale("unknown", day=-3), sold_by_me=None),
        replace(buy("other", day=-3), bought_by_me=False),
        buy("second", day=-3, price=1_200_000),
        replace(sale("foreign", day=-3), sold_by_me=False),
        replace(sale("invalid", day=-3), kind=99),
    ],
)
def test_intermediate_transfer_never_bridges_old_acquisition(interruption):
    data = build_profit([buy(), interruption, sale()], [], NOW)
    assert data.closed[0].purchase_price == (1_200_000 if interruption.id == "second" else None)
    assert data.realized == (100_000 if interruption.id == "second" else None)


def test_two_distinct_transfers_at_same_time_are_ambiguous():
    data = build_profit([buy(day=-1), sale()], squad(), NOW)
    assert data.realized is None and data.open_gain is None


def test_missing_purchase_future_and_old_sales_do_not_create_profit():
    data = build_profit([sale(), buy("future", day=1)], squad(), NOW)
    assert data.realized is None and data.closed[0].gain is None
    data = build_profit([buy(day=-90), sale(day=-2)], [], NOW)
    assert data.realized == 200_000  # purchase before reporting window is still usable
    assert not build_profit([buy(day=-90), sale(day=-61)], [], NOW).closed


def test_sale_to_another_manager_requires_own_seller_identity():
    data = build_profit([buy(), replace(sale(), kind=1)], [], NOW)
    assert data.realized == 200_000
    assert build_profit([buy(), replace(sale(), kind=1, sold_by_me=None)], [], NOW).realized is None


def test_unknown_seller_never_becomes_own_sale():
    data = build_profit([buy(), replace(sale(), sold_by_me=None)], [], NOW)
    assert data.unknown_sellers == 1 and not data.closed
    assert not build_profit([replace(sale(), sold_by_me=False)], [], NOW).closed


def test_conflicting_own_buyer_and_seller_is_not_counted():
    data = build_profit([buy(), replace(sale(), kind=1, bought_by_me=True)], squad(), NOW)
    assert data.realized is None and data.open_gain is None


def row(e, league=TEAM_LEAGUE_ID):
    return LeagueTransfer(
        activity_id=e.id,
        league_id=league,
        player_id=e.player_id,
        player_name=e.name,
        team_id="14",
        at=e.at,
        kind=e.kind,
        price=e.price,
        by_me=e.bought_by_me,
        sold_by_me=e.sold_by_me,
    )


async def test_database_view_scoped_and_unchanged_after_multiple_reads(sessions):
    async with sessions.begin() as session:
        session.add_all([row(buy()), row(sale()), row(buy("foreign", player="9"), "other")])
    data = await load_profit(sessions, TEAM_LEAGUE_ID, [], NOW)
    assert data.realized == 200_000 and data.collection_warning
    assert await load_profit(sessions, TEAM_LEAGUE_ID, [], NOW) == data
    assert (await load_profit(sessions, "other", [], NOW)).realized is None


async def test_cached_actor_backfill_requires_exact_event_and_league(sessions):
    event = sale()
    feed = {
        "af": [
            {
                "i": "s",
                "t": 15,
                "dt": event.at.isoformat(),
                "data": {
                    "pi": "1",
                    "pn": "Fixture Spieler",
                    "t": 2,
                    "trp": event.price,
                    "slr": "Manager 1",
                },
            }
        ]
    }
    async with sessions.begin() as session:
        session.add(row(replace(event, sold_by_me=None)))
        session.add(
            ApiCache(
                key=cache_key(
                    f"/v4/leagues/{TEAM_LEAGUE_ID}/activitiesFeed",
                    {"filter": 15, "start": 0, "max": 50},
                ),
                payload=feed,
                fetched_at=NOW,
                expires_at=NOW,
            )
        )
    assert await backfill_sellers_from_cache(sessions, "other", "Manager 1") == 0
    assert await backfill_sellers_from_cache(sessions, TEAM_LEAGUE_ID, "") == 0
    conflicting = (
        m.ActivityFeed.model_validate(feed).items
        + m.ActivityFeed.model_validate(
            {"af": [feed["af"][0] | {"data": feed["af"][0]["data"] | {"slr": "Manager 2"}}]}
        ).items
    )
    assert await identify_sellers(sessions, TEAM_LEAGUE_ID, conflicting, "Manager 1") == 0
    assert (
        await identify_sellers(
            sessions,
            TEAM_LEAGUE_ID,
            m.ActivityFeed.model_validate(
                {"af": [feed["af"][0] | {"data": feed["af"][0]["data"] | {"trp": 999}}]}
            ).items,
            "Manager 1",
        )
        == 0
    )
    assert await backfill_sellers_from_cache(sessions, TEAM_LEAGUE_ID, "Manager 1") == 1
    assert await backfill_sellers_from_cache(sessions, TEAM_LEAGUE_ID, "Manager 1") == 0
    async with sessions() as session:
        assert (await session.get(LeagueTransfer, "s")).sold_by_me is True
    assert (
        await load_profit(sessions, TEAM_LEAGUE_ID, [], NOW)
    ).realized is None  # seller known, cost unknown


@pytest.mark.parametrize(
    "status,hours,detail,warning",
    [
        ("ok", 1, None, False),
        ("failed", 1, None, True),
        ("ok", 7, None, True),
        ("ok", 1, "1 nicht lesbar", True),
    ],
)
async def test_collection_health_is_visible(sessions, status, hours, detail, warning):
    async with sessions.begin() as session:
        session.add(
            JobRun(
                job="collect_league_transfers",
                started_at=NOW - timedelta(hours=hours),
                finished_at=NOW - timedelta(hours=hours),
                status=status,
                detail=detail,
            )
        )
    assert (
        bool((await load_profit(sessions, TEAM_LEAGUE_ID, [], NOW)).collection_warning) == warning
    )


def test_profit_page_does_not_collect_transfers(logged_in_client, fake_kickbase, settings):
    engine = create_engine(settings.database_url)
    with Session(engine) as session, session.begin():
        session.add_all([row(buy(day=-4)), row(sale(day=-1))])
    engine.dispose()
    response = logged_in_client.get("/empfehlungen/gewinne")
    assert response.status_code == 200
    assert "Realisiert" in response.text and "200" in response.text
    assert "Kaufpreis einschließlich bezahltem Aufschlag" in response.text
    assert not any("activitiesFeed" in r.url.path for r in fake_kickbase.requests)
    assert all(r.method == "GET" or r.url.path == "/v4/user/login" for r in fake_kickbase.requests)


def test_profit_page_handles_missing_data(logged_in_client):
    response = logged_in_client.get("/empfehlungen/gewinne")
    assert "bedeutet nicht, dass dein tatsächlicher Gewinn null ist" in response.text


def test_profit_page_handles_outage(logged_in_client, fake_kickbase):
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
    assert (
        "Kickbase ist gerade nicht erreichbar" in logged_in_client.get("/empfehlungen/gewinne").text
    )
