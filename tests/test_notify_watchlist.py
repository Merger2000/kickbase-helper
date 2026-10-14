"""Tests for the watchlist alarm logic (Block C, C1.3).

The rule layer must work in isolation: it never touches the Kickbase API or the
DB. Each table-test exercises one trigger (and the obvious "should not fire"
case), then a few cross-cutting tests pin the dedupe + baseline behaviour that
the rest of the system relies on."""

from datetime import UTC, date, datetime

import pytest

from app.db.models import Watchlist
from app.kickbase import models as m
from app.lineups.service import TeamPrediction
from app.notify.events import SENT_KEY, WatchlistConfig, WatchlistRule
from app.notify.watchlist import (
    WATCHLIST_LINEUP_KEY,
    WATCHLIST_STATUS_KEY,
    Collected,
    collect_watchlist_events,
    commit_watchlist,
    lineup_dropped_events,
    listing_events,
    load_watchlist_config,
    price_events,
    status_events,
)
from app.services.watchlist import WatchlistItem
from app.web.formatting import NBSP
from tests.fakes import MemoryKV

BASE = "https://kickbase.example.com"
NOW = datetime(2026, 10, 9, 20, 45, tzinfo=UTC)
TODAY = date(2026, 10, 9)

ALL_ON = WatchlistConfig()
LISTING_OFF = WatchlistConfig(listing=WatchlistRule(enabled=False))
PRICE_OFF = WatchlistConfig(price_below=WatchlistRule(enabled=False))
STATUS_OFF = WatchlistConfig(status_changes=WatchlistRule(enabled=False))
LINEUP_OFF = WatchlistConfig(lineup_dropped=WatchlistRule(enabled=False))


# ---------------------------------------------------------------- fixtures


def _entry(player_id: str, price_below: int | None = None) -> Watchlist:
    return Watchlist(
        league_id="league-1",
        player_id=player_id,
        added_at=NOW,
        price_below=price_below,
    )


def item(
    pid: str = "1",
    name: str = "Hansen",
    mv: int = 4_000_000,
    status: int = 0,
    price_below: int | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        entry=_entry(pid, price_below),
        player_name=name,
        market_value=mv,
        status=status,
    )


def market_player(
    pid: str = "1",
    name: str = "Hansen",
    price: int = 1_500_000,
    mv: int = 4_000_000,
    status: int = 0,
    listed_at: datetime | None = datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
) -> m.MarketPlayer:
    raw = {
        "i": pid,
        "fn": "",
        "n": name,
        "tid": "t1",
        "pos": 1,
        "st": status,
        "mv": mv,
        "mvt": 0,
        "prc": price,
        "exs": None,
        "dt": listed_at.isoformat() if listed_at else None,
    }
    return m.MarketPlayer.model_validate(raw)


# ---------------------------------------------------------------- listings


def test_listing_event_fires_when_player_newly_on_market():
    player = market_player(pid="1", price=2_500_000)
    events = listing_events(
        [item(pid="1")],
        [player],
        squad_ids=set(),
        my_offer_listing_keys=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 1
    assert events[0].key == f"watchlist.listing:{player.listing_key}"
    assert "Hansen" in events[0].text
    assert "2,50 Mio." in events[0].text or "2,50" in events[0].text
    assert "🛒" in events[0].text


def test_listing_event_skipped_for_own_squad():
    events = listing_events(
        [item(pid="1")],
        [market_player(pid="1")],
        squad_ids={"1"},
        my_offer_listing_keys=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_listing_event_skipped_when_user_bid_on_listing():
    player = market_player(pid="1")
    events = listing_events(
        [item(pid="1")],
        [player],
        squad_ids=set(),
        my_offer_listing_keys={player.listing_key},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_listing_event_disabled_by_config():
    events = listing_events(
        [item(pid="1")],
        [market_player(pid="1")],
        squad_ids=set(),
        my_offer_listing_keys=set(),
        rule=WatchlistRule(enabled=False),
        base_url=BASE,
    )
    assert events == []


def test_listing_event_no_market_row_means_no_event():
    events = listing_events(
        [item(pid="1")],
        [market_player(pid="2")],
        squad_ids=set(),
        my_offer_listing_keys=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


# ---------------------------------------------------------------- price


def test_price_below_fires_on_market_offer():
    events = price_events(
        [item(pid="1", price_below=2_000_000)],
        [market_player(pid="1", price=1_500_000)],
        squad_ids=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 1
    assert "💸" in events[0].text
    assert "Marktangebot" in events[0].text
    assert ("1,50" + NBSP + "Mio.") in events[0].text
    assert ("Schwelle 2,00" + NBSP + "Mio.") in events[0].text


def test_price_below_falls_back_to_market_value_when_not_listed():
    events = price_events(
        [item(pid="1", price_below=5_000_000, mv=4_000_000)],
        [market_player(pid="2")],
        squad_ids=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 1
    assert "💰" in events[0].text
    assert "Marktwert" in events[0].text


def test_price_below_skipped_when_price_above_threshold():
    events = price_events(
        [item(pid="1", price_below=2_000_000)],
        [market_player(pid="1", price=3_000_000)],
        squad_ids=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_price_below_skipped_when_no_threshold():
    events = price_events(
        [item(pid="1", price_below=None)],
        [market_player(pid="1", price=1)],
        squad_ids=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_price_below_skipped_for_own_squad():
    events = price_events(
        [item(pid="1", price_below=5_000_000)],
        [market_player(pid="1", price=1_000_000)],
        squad_ids={"1"},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_price_below_disabled_by_config():
    events = price_events(
        [item(pid="1", price_below=5_000_000)],
        [market_player(pid="1", price=1_000_000)],
        squad_ids=set(),
        rule=WatchlistRule(enabled=False),
        base_url=BASE,
    )
    assert events == []


# ---------------------------------------------------------------- status


def test_status_change_fires_both_ways():
    events = status_events(
        [item(pid="1", name="Maza", status=2), item(pid="2", name="Sano", status=0)],
        previous={"1": 0, "2": 1},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 2
    texts = sorted(event.text for event in events)
    assert any("🚑" in t and "Maza" in t and "fit → angeschlagen" in t for t in texts)
    assert any("✅" in t and "Sano" in t and "verletzt → fit" in t for t in texts)


def test_status_change_skipped_for_new_watchlist_entries():
    events = status_events(
        [item(pid="1", status=2)],
        previous={},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_status_change_skipped_when_unchanged():
    events = status_events(
        [item(pid="1", status=0)],
        previous={"1": 0},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_status_change_disabled_by_config():
    events = status_events(
        [item(pid="1", status=2)],
        previous={"1": 0},
        rule=WatchlistRule(enabled=False),
        base_url=BASE,
    )
    assert events == []


# ---------------------------------------------------------------- lineup


def test_lineup_dropped_fires_when_player_left_the_xi():
    team = TeamPrediction(starting=frozenset())
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={"t1": team},
        previous_in_xi={"1": True},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 1
    assert "⚠️" in events[0].text
    assert "Hansen" in events[0].text


def test_lineup_dropped_skipped_when_player_was_not_in_xi():
    team = TeamPrediction(starting=frozenset())
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={"t1": team},
        previous_in_xi={"1": False},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_lineup_dropped_skipped_when_player_still_in_xi():
    team = TeamPrediction(starting=frozenset({"1"}))
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={"t1": team},
        previous_in_xi={"1": True},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_lineup_dropped_ignores_clubs_without_prediction():
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={},
        previous_in_xi={"1": True},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_lineup_dropped_ignores_players_with_unknown_team():
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={},
        predicted={"t1": TeamPrediction(starting=frozenset())},
        previous_in_xi={"1": True},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert events == []


def test_lineup_dropped_disabled_by_config():
    team = TeamPrediction(starting=frozenset())
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={"t1": team},
        previous_in_xi={"1": True},
        rule=WatchlistRule(enabled=False),
        base_url=BASE,
    )
    assert events == []


def test_lineup_dropped_fires_when_only_alternative():
    team = TeamPrediction(starting=frozenset(), alternatives=frozenset({"1"}))
    events = lineup_dropped_events(
        [item(pid="1")],
        team_by_player={"1": "t1"},
        predicted={"t1": team},
        previous_in_xi={"1": True},
        rule=WatchlistRule(),
        base_url=BASE,
    )
    assert len(events) == 1


# ---------------------------------------------------------------- collect / commit


async def test_first_run_only_records_status_and_lineup_baseline():
    """First watchlist run must not fire status or lineup alarms – the baseline
    is built up silently, like notify_squad."""
    kv = MemoryKV()
    team = TeamPrediction(starting=frozenset({"1"}))
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1", status=2)],
        market=[],
        predicted={"t1": team},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={"1": "t1"},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    # Status: previously 0, player now injured → would normally fire
    # Lineup: previously in XI, player now dropped → would normally fire
    # First run: no baseline → both suppressed
    status_events = [e for e in collected.events if e.key.startswith("watchlist.status:")]
    lineup_events = [e for e in collected.events if e.key.startswith("watchlist.lineup:")]
    assert status_events == []
    assert lineup_events == []
    # But the new baseline is ready for the next run.
    assert collected.statuses == {"1": 2}
    assert collected.lineups == {"1": True}


async def test_listing_and_price_alarms_fire_on_first_run():
    """Unlike status/lineup, listings and prices are point-in-time facts about
    the world, not transitions – the first run already warns."""
    kv = MemoryKV()
    collected = await collect_watchlist_events(
        watchlist=[
            item(pid="1"),
            item(pid="2", price_below=2_000_000, mv=3_000_000),
        ],
        market=[
            market_player(pid="1", price=2_500_000),
            market_player(pid="2", price=1_500_000),
        ],
        predicted={},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    keys = {event.key for event in collected.events}
    assert any(k.startswith("watchlist.listing:") for k in keys)
    assert any(k.startswith("watchlist.price:") for k in keys)


async def test_alarms_are_deduped_via_shared_sent_key():
    kv = MemoryKV()
    squad = [item(pid="1")]
    market = [market_player(pid="1", price=2_000_000)]
    first = await collect_watchlist_events(
        watchlist=squad,
        market=market,
        predicted={},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    assert first.events
    await commit_watchlist(kv, first, TODAY)
    second = await collect_watchlist_events(
        watchlist=squad,
        market=market,
        predicted={},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    assert second.events == []


async def test_status_change_fires_after_baseline_is_set():
    kv = MemoryKV()
    kv.data[WATCHLIST_STATUS_KEY] = {"1": 0}
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1", status=2)],
        market=[],
        predicted={},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    [event] = collected.events
    assert event.key == "watchlist.status:1:0>2"
    assert "fit → angeschlagen" in event.text


async def test_lineup_dropped_fires_after_baseline_is_set():
    kv = MemoryKV()
    kv.data[WATCHLIST_LINEUP_KEY] = {"1": True}
    team = TeamPrediction(starting=frozenset())
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1")],
        market=[],
        predicted={"t1": team},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={"1": "t1"},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    [event] = collected.events
    assert event.key == "watchlist.lineup:1"


async def test_own_squad_and_own_offers_silenced_in_collect():
    kv = MemoryKV()
    offered = market_player(pid="1")
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1")],
        market=[offered],
        predicted={},
        squad_ids={"1"},
        my_offer_listing_keys={offered.listing_key},
        team_by_player={},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    listing = [e for e in collected.events if e.key.startswith("watchlist.listing:")]
    assert listing == []


async def test_commit_records_baselines_and_keeps_sent_keys():
    kv = MemoryKV()
    kv.data[WATCHLIST_STATUS_KEY] = {"1": 0}
    team = TeamPrediction(starting=frozenset({"1"}))
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1", status=2)],
        market=[],
        predicted={"t1": team},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={"1": "t1"},
        config=ALL_ON,
        kv=kv,
        base_url=BASE,
    )
    [event] = collected.events
    await commit_watchlist(kv, collected, TODAY)
    assert kv.data[WATCHLIST_STATUS_KEY] == {"1": 2}
    assert kv.data[WATCHLIST_LINEUP_KEY] == {"1": True}
    assert kv.data[SENT_KEY][event.key] == TODAY.isoformat()


async def test_commit_prunes_old_sent_keys():
    """Old SENT entries older than KEEP_SENT are dropped, just like notify_squad."""
    from app.notify.events import KEEP_SENT

    kv = MemoryKV()
    kv.data[WATCHLIST_STATUS_KEY] = {}
    kv.data[WATCHLIST_LINEUP_KEY] = {}
    kv.data[SENT_KEY] = {"old": "2026-01-01"}
    await commit_watchlist(kv, Collected(), TODAY)
    assert "old" not in kv.data[SENT_KEY]
    assert TODAY - KEEP_SENT < TODAY  # sanity, no static value


async def test_load_watchlist_config_defaults_to_all_alarms_on():
    cfg = load_watchlist_config()
    assert cfg.listing.enabled is True
    assert cfg.price_below.enabled is True
    assert cfg.status_changes.enabled is True
    assert cfg.lineup_dropped.enabled is True


async def test_config_disabled_rules_stay_disabled_through_collect():
    kv = MemoryKV()
    team = TeamPrediction(starting=frozenset())
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1", price_below=10_000_000, status=2)],
        market=[market_player(pid="1", price=500_000)],
        predicted={"t1": team},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={"1": "t1"},
        config=WatchlistConfig(
            listing=WatchlistRule(enabled=False),
            price_below=WatchlistRule(enabled=False),
            status_changes=WatchlistRule(enabled=False),
            lineup_dropped=WatchlistRule(enabled=False),
        ),
        kv=kv,
        base_url=BASE,
    )
    assert collected.events == []


async def test_message_includes_player_link():
    """Watchlist events should link to the player page, like notify_squad events."""
    events = listing_events(
        [item(pid="1")],
        [market_player(pid="1", name="<b>Böse</b>")],
        squad_ids=set(),
        my_offer_listing_keys=set(),
        rule=WatchlistRule(),
        base_url=BASE,
    )
    [event] = events
    assert f"{BASE}/spieler/1" in event.text
    assert "&lt;b&gt;Böse&lt;/b&gt;" in event.text


@pytest.mark.parametrize(
    "rule",
    [
        ("listing", LISTING_OFF, ["watchlist.listing:"]),
        ("price_below", PRICE_OFF, ["watchlist.price:"]),
        ("status_changes", STATUS_OFF, ["watchlist.status:"]),
        ("lineup_dropped", LINEUP_OFF, ["watchlist.lineup:"]),
    ],
)
async def test_each_rule_can_be_disabled_individually(rule):
    name, config, blocked = rule
    kv = MemoryKV()
    kv.data[WATCHLIST_STATUS_KEY] = {"1": 0}
    kv.data[WATCHLIST_LINEUP_KEY] = {"1": True}
    team = TeamPrediction(starting=frozenset())
    collected = await collect_watchlist_events(
        watchlist=[item(pid="1", price_below=10_000_000, status=2)],
        market=[market_player(pid="1", price=500_000)],
        predicted={"t1": team},
        squad_ids=set(),
        my_offer_listing_keys=set(),
        team_by_player={"1": "t1"},
        config=config,
        kv=kv,
        base_url=BASE,
    )
    blocked_events = [e for e in collected.events if any(e.key.startswith(p) for p in blocked)]
    assert blocked_events == [], f"rule {name} should be disabled but fired {blocked_events}"
    # And the *other* rules still fire (only one is disabled per parameter).
    assert collected.events != [], f"rule {name}: everything disabled by mistake"
