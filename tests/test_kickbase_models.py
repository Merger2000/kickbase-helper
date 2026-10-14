"""Pydantic model tests for the Kickbase API responses.

The fixture-driven happy path lives in `test_kickbase_endpoints.py`; this file
covers shape-only behaviour that does not need a client (Block C, C2.1:
`MarketOffer` + `MarketPlayer.offers` / `best_offer`)."""

import pytest

from app.kickbase import models as m
from tests.fakes import fixture


def _caci() -> m.MarketPlayer:
    market = m.Market.model_validate(fixture("league_market"))
    return next(p for p in market.players if p.id == "3470")


def _batz() -> m.MarketPlayer:
    market = m.Market.model_validate(fixture("league_market"))
    return next(p for p in market.players if p.id == "577")


def test_market_offer_parses_fixture_amount_bidder_and_status():
    [offer] = _caci().offers
    assert offer.amount == 8_500_000
    assert offer.bidder_id == "9000001"
    assert offer.status == 0


def test_market_offer_does_not_keep_bidder_name():
    """`unm` is another manager's name – must not surface as an attribute."""
    [offer] = _caci().offers
    assert "name" not in offer.model_fields_set
    assert not hasattr(offer, "name")


def test_market_player_without_offs_has_empty_offers_and_none_best_offer():
    batz = _batz()
    assert batz.offers == []
    assert batz.best_offer is None


def test_market_player_with_offs_exposes_best_offer():
    assert _caci().best_offer.amount == 8_500_000


def test_market_player_without_offs_default_is_empty_list():
    """Synthetic case (the fixture is mostly entries without offers) – confirms the
    `default_factory` path even when the input omits `ofs` entirely."""
    raw = {
        "i": "1",
        "fn": "A",
        "n": "B",
        "tid": "t1",
        "pos": 1,
        "st": 0,
        "mv": 1_000_000,
        "mvt": 0,
        "prc": 1_000_000,
    }
    player = m.MarketPlayer.model_validate(raw)
    assert player.offers == []
    assert player.best_offer is None


def test_best_offer_picks_highest_amount():
    raw = {
        "i": "1",
        "fn": "",
        "n": "B",
        "tid": "t1",
        "pos": 1,
        "st": 0,
        "mv": 1_000_000,
        "mvt": 0,
        "prc": 1_000_000,
        "ofs": [
            {"uop": 100, "uoid": "u-low", "st": 0},
            {"uop": 999, "uoid": "u-high", "st": 0},
            {"uop": 500, "uoid": "u-mid", "st": 0},
        ],
    }
    player = m.MarketPlayer.model_validate(raw)
    assert [o.amount for o in player.offers] == [100, 999, 500]
    best = player.best_offer
    assert best is not None and best.amount == 999
    assert best.bidder_id == "u-high"


def test_market_offer_accepts_bidder_id_via_u_alias():
    """Spec notes both `uoid` and `u` carry the bidder id – the model must accept either."""
    raw = {"uop": 100, "u": "9000001", "st": 0}
    offer = m.MarketOffer.model_validate(raw)
    assert offer.bidder_id == "9000001"
    assert offer.amount == 100
    assert offer.status == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"uop": 100.0, "uoid": "u", "st": 0}, 100),  # `Money` rounds floats
        ({"uop": 999_999.6, "uoid": "u", "st": 0}, 1_000_000),
    ],
)
def test_market_offer_amount_is_money(raw, expected):
    assert m.MarketOffer.model_validate(raw).amount == expected


# ---------------------------------------------------------------------------- user teamcenter


def _user_teamcenter() -> m.UserTeamCenter:
    return m.UserTeamCenter.model_validate(fixture("league_user_teamcenter"))


def test_user_teamcenter_parses_fixture():
    center = _user_teamcenter()
    assert center.name == "Manager 1"
    assert center.ppc == 0
    assert center.clpc == 11
    assert len(center.lineup) == 11  # 1 GK, 3 DEF, 4 MID, 3 FWD – the Bundesliga lineup
    assert center.bench == []
    assert all(isinstance(p, m.UserTeamCenterPlayer) for p in center.lineup)
    assert {u.id for u in center.users} == {
        "9000001",  # the requester (also the lineup owner)
        "9000002",
        "9000003",
        "9000004",
        "9000005",
        "9000006",
    }


def test_user_teamcenter_player_ids_coerced_from_int():
    """The endpoint sends `tid` and `mi` as ints (unlike the rest of the API). The
    `IdStr` validator must coerce them to str so downstream code can treat ids uniformly."""
    center = _user_teamcenter()
    caci = next(p for p in center.lineup if p.last_name == "Caci")
    assert caci.team_id == "18"
    assert caci.match_id == "12031"
    # `i` is a player id that the API consistently sends as int here.
    assert caci.id == "3470"


def test_user_teamcenter_empty_bench_is_kept():
    # The endpoint leaves out the bench when no substitutes played; default_factory makes
    # the missing list distinguishable from an empty list returned by the server.
    assert _user_teamcenter().bench == []


def test_user_teamcenter_player_played_property():
    center = _user_teamcenter()
    started = [p for p in center.lineup if p.match_status == 2]
    assert started and all(p.played for p in started)
    # Synthetic entry: `mst` None means the match is still scheduled (future matchday).
    raw = {"i": "1", "n": "X", "tid": "1", "mst": None}
    assert m.UserTeamCenterPlayer.model_validate(raw).played is False
    # And anything else (0 = scheduled, …) is also "not finished".
    assert m.UserTeamCenterPlayer.model_validate({**raw, "mst": 0}).played is False


def test_user_teamcenter_player_name_and_points():
    caci = next(p for p in _user_teamcenter().lineup if p.last_name == "Caci")
    assert caci.name == "Caci"
    assert caci.points == 311
    assert caci.participation == 5  # kicker code, not the squad status


# ---------------------------------------------------------------------------- ranking day


def test_ranking_day_parses_fixture_and_matches_default_ranking_shape():
    day_ranking = m.Ranking.model_validate(fixture("league_ranking_day"))
    default_ranking = m.Ranking.model_validate(fixture("league_ranking"))
    # Same fields, same user ids (anonymised the same way), but the day-specific snapshot
    # exposes the same `latest_matchday` and team values for the played matchday.
    assert day_ranking.latest_matchday == 5
    assert day_ranking.league_name == "Testliga"
    assert [u.id for u in day_ranking.users] == [u.id for u in default_ranking.users]
    me = day_ranking.for_user("9000001")
    assert me is not None and me.matchday_points == 934
