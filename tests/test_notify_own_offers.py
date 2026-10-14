"""Tests for the own-offers alarm logic (Block C, C2.2).

The rule layer is pure: no Kickbase or DB I/O. Each table-test exercises one
trigger or one suppression; a few integration tests pin the dedupe key shape
and the text format. Bidder names are private and must never surface as text."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.kickbase import models as m
from app.notify.events import OwnOffersRule
from app.notify.own_offers import own_offer_events
from app.web.formatting import NBSP
from tests.fakes import fixture

BASE = "https://kickbase.example.com"
TZ = ZoneInfo("Europe/Berlin")
FETCHED_AT = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)  # 20:00 local
NOW = datetime(2026, 10, 9, 18, 30, tzinfo=UTC)
ALL_ON = OwnOffersRule()
DISABLED = OwnOffersRule(enabled=False)
AT_FIVE_PCT = OwnOffersRule(min_premium_pct=5)


# ---------------------------------------------------------------- helpers


def _listed_player(
    pid: str = "1",
    name: str = "Maza",
    *,
    mv: int = 4_000_000,
    bid: int | None = 8_500_000,
    seller_id: str | None = "my-id",
    bidder_id: str = "9000002",
    expires_in_seconds: int | None = 3600,
    listed_at: datetime | None = datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
) -> m.MarketPlayer:
    raw: dict = {
        "i": pid,
        "fn": "",
        "n": name,
        "tid": "t1",
        "pos": 4,
        "st": 0,
        "mv": mv,
        "mvt": 0,
        "prc": mv,
        "exs": expires_in_seconds,
        "dt": listed_at.isoformat() if listed_at else None,
    }
    if seller_id is not None:
        raw["u"] = {"i": seller_id, "n": "Manager Inhaber"}
    if bid is not None:
        raw["ofs"] = [
            {"uop": bid, "uoid": bidder_id, "st": 0, "unm": "Manager Bieter"},
        ]
    return m.MarketPlayer.model_validate(raw)


# ---------------------------------------------------------------- fixtures from the live API


def test_caci_offers_in_fixture_have_no_own_seller():
    """Real fixture sanity check: no market entry in `league_market.json` is sold by
    the manager (`9000001`). The rule must therefore produce zero events."""
    market = m.Market.model_validate(fixture("league_market"))
    events = own_offer_events(market.players, "9000001", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


# ---------------------------------------------------------------- basic triggers


def test_event_fires_when_own_player_gets_bid_over_market_value():
    player = _listed_player(bid=4_500_000, mv=4_000_000)
    events = own_offer_events([player], "my-id", OwnOffersRule(), FETCHED_AT, TZ, BASE, now=NOW)
    assert len(events) == 1
    [event] = events
    assert event.key.startswith("own_offers:")
    assert event.key.endswith(":4500000")
    # Premium is +500k / +12.5 % over 4.0 Mio. market value; expires at 21:00 local.
    assert "Maza" in event.text
    assert "+12,5" in event.text
    assert ("+500" + NBSP + "Tsd.") in event.text
    assert "annehmen nur in der Kickbase-App" in event.text
    assert "läuft bis 21:00" in event.text


def test_bidder_name_never_appears_in_text():
    """`unm` is another manager's private name – must never reach the Telegram text."""
    player = _listed_player(bidder_id="u-secret", bid=5_000_000, mv=4_000_000)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert "Manager" not in event.text
    assert "u-secret" not in event.text
    assert "<a href" in event.text  # the player link is still there


def test_html_in_player_name_is_escaped():
    player = _listed_player(name="<b>Böse</b>", bid=5_000_000, mv=4_000_000)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert "&lt;b&gt;Böse&lt;/b&gt;" in event.text


# ---------------------------------------------------------------- suppressions


def test_bid_exactly_at_market_value_does_not_fire():
    """`best_offer.amount <= MV` (threshold factor = 1): no premium, no event."""
    player = _listed_player(bid=4_000_000, mv=4_000_000)
    events = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_bid_below_market_value_does_not_fire():
    player = _listed_player(bid=3_500_000, mv=4_000_000)
    events = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_foreign_seller_does_not_fire():
    """The listing belongs to another manager – not interesting for the alarm."""
    player = _listed_player(seller_id="9000002", bid=5_000_000, mv=4_000_000)
    events = own_offer_events([player], "9000001", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_kickbase_seller_does_not_fire():
    """`seller is None` means Kickbase sells the player directly – no premium to alert."""
    player = _listed_player(seller_id=None, bid=5_000_000, mv=4_000_000)
    events = own_offer_events([player], "9000001", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_no_offers_does_not_fire():
    """A listing nobody has bid on is no opportunity – nothing to alert."""
    player = _listed_player(bid=None)
    events = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_open_ended_listing_shows_ohne_ablauf():
    """`expires_in_seconds` is None for manager listings – show a clear label, not a crash."""
    player = _listed_player(bid=5_000_000, mv=4_000_000, expires_in_seconds=None)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert "ohne Ablauf" in event.text
    assert "läuft bis" not in event.text


def test_expiry_shows_date_when_not_today():
    """A bid expiring tomorrow gets the day in the expiry label."""
    player = _listed_player(bid=5_000_000, mv=4_000_000, expires_in_seconds=60 * 60 * 14)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert "10.10." in event.text


# ---------------------------------------------------------------- config knobs


def test_disabled_rule_silences_everything():
    player = _listed_player(bid=9_000_000, mv=4_000_000)
    events = own_offer_events([player], "my-id", DISABLED, FETCHED_AT, TZ, BASE, now=NOW)
    assert events == []


def test_min_premium_pct_filters_lowball_bids():
    """At 5 % the +12.5 % bid fires, but a +3 % bid does not."""
    low = _listed_player(bid=4_120_000, mv=4_000_000)
    high = _listed_player(pid="2", bid=4_500_000, mv=4_000_000)
    events = own_offer_events([low, high], "my-id", AT_FIVE_PCT, FETCHED_AT, TZ, BASE, now=NOW)
    assert len(events) == 1
    [event] = events
    assert ":4500000" in event.key


# ---------------------------------------------------------------- dedupe key shape


def test_higher_follow_up_bid_creates_a_new_event_key():
    """The spec demands "höheres Gebot = neuer Alarm": the dedupe key includes the amount,
    so a second run with a higher bid is not silenced by the previous one."""
    player = _listed_player(bid=4_500_000, mv=4_000_000)
    first = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert len(first) == 1
    assert first[0].key == f"own_offers:{player.listing_key}:4500000"

    # Same listing, a higher bid on the next poll: new amount → new key → fires again.
    player = _listed_player(bid=4_700_000, mv=4_000_000)
    second = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert len(second) == 1
    assert second[0].key == f"own_offers:{player.listing_key}:4700000"
    assert second[0].key != first[0].key


def test_same_bid_listing_key_is_stable_across_runs():
    """Two runs of the exact same (listing, amount) pair produce the same dedupe key,
    so the shared `SENT_KEY` from `app.notify.events` can filter it out."""
    player = _listed_player(bid=4_500_000, mv=4_000_000)
    [a] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    [b] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert a.key == b.key


# ---------------------------------------------------------------- multi-player


def test_only_own_listings_produce_events_in_a_mixed_market():
    own = _listed_player(pid="1", bid=5_000_000, mv=4_000_000, seller_id="my-id")
    foreign = _listed_player(pid="2", bid=5_000_000, mv=4_000_000, seller_id="other")
    kickbase = _listed_player(pid="3", bid=5_000_000, mv=4_000_000, seller_id=None)
    events = own_offer_events(
        [own, foreign, kickbase], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW
    )
    assert len(events) == 1
    assert "Maza" in events[0].text


def test_highest_offer_among_several_is_the_one_computed_against_mv():
    """`best_offer` returns the highest amount; the premium is based on that one."""
    raw = {
        "i": "1",
        "fn": "",
        "n": "Maza",
        "tid": "t1",
        "pos": 4,
        "st": 0,
        "mv": 4_000_000,
        "mvt": 0,
        "prc": 4_000_000,
        "exs": 3600,
        "dt": "2026-10-09T12:00:00Z",
        "u": {"i": "my-id", "n": "ich"},
        "ofs": [
            {"uop": 4_300_000, "uoid": "low", "st": 0},
            {"uop": 5_100_000, "uoid": "high", "st": 0},
            {"uop": 4_700_000, "uoid": "mid", "st": 0},
        ],
    }
    player = m.MarketPlayer.model_validate(raw)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    # +1.1 Mio / +27.5 % (1_100_000 / 4_000_000 * 100) – the 5.10 Mio bid drives it.
    assert "5,10" in event.text
    assert "+27,5" in event.text
    assert "+1,10" in event.text
    assert ":5100000" in event.key


# ---------------------------------------------------------------- text formatting


def test_premium_uses_euro_unit_below_thousand():
    """Tiny amounts (e.g. 500 € over a 500 Tsd. MV) must not pretend to be a thousand."""
    player = _listed_player(bid=500_500, mv=500_000)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert (f"+500{NBSP}€") in event.text


def test_text_contains_the_player_page_link():
    player = _listed_player(pid="42", bid=5_000_000, mv=4_000_000)
    [event] = own_offer_events([player], "my-id", ALL_ON, FETCHED_AT, TZ, BASE, now=NOW)
    assert f"{BASE}/spieler/42" in event.text
