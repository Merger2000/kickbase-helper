from dataclasses import replace

import pytest

from app.kickbase.models import Budget, Position
from app.services.players import StartProbability
from app.services.recommendations import Candidate, Recommendation
from app.services.scoring import Score, load_scoring_config
from app.services.transfer_simulation import (
    PlanOffer,
    SimulationError,
    reserve_from,
    simulate_transfers,
)

PROFILE = load_scoring_config().profiles["aufstellung"]


def rec(pid, position=Position.FORWARD, value=1_000_000, *, start=90, status=0):
    return Recommendation(
        Candidate(pid, f"Player {pid}", "1", "Test", position, status, None),
        Score(50, (), False),
        value,
        start=StartProbability(start, ("fixture",)),
    )


def full_squad():
    return (
        [rec("1", Position.GOALKEEPER)]
        + [rec(str(i), Position.DEFENDER) for i in range(2, 6)]
        + [rec(str(i), Position.MIDFIELDER) for i in range(6, 10)]
        + [rec(str(i), Position.FORWARD) for i in range(10, 12)]
    )


def offer(pid="20", *, cost=2_000_000, bid=0, timed=True, position=Position.FORWARD):
    return PlanOffer(replace(rec(pid, position), price=cost), cost, bid, timed)


def simulate(*, squad=None, offers=(), budget=10_000_000, reserve=0, sales=(), buys=(), costs=None):
    return simulate_transfers(
        full_squad() if squad is None else squad,
        offers,
        budget,
        reserve,
        sales,
        buys,
        costs or {},
        PROFILE,
    )


def test_existing_bid_counted_once_and_other_bids_remain_reserved():
    result = simulate(offers=[offer(bid=1_500_000)], reserve=3_000_000, buys=["20"])
    assert result.budget == 8_000_000
    assert result.released_bids == 1_500_000
    assert result.remaining_bids == 1_500_000
    assert result.budget_after_bids == 6_500_000


def test_multiple_selected_bids_and_sales_are_combined():
    result = simulate(
        offers=[offer(bid=1_000_000), offer("21", cost=3_000_000, bid=2_000_000)],
        reserve=4_000_000,
        sales=["10", "11"],
        buys=["20", "21"],
    )
    assert result.revenue == 2_000_000
    assert result.cost == 5_000_000
    assert result.budget_after_bids == 6_000_000
    assert result.roster_size == 11
    assert result.gaps == 0


def test_selling_only_keeper_leaves_a_gap_even_with_more_expensive_forward():
    squad = full_squad()
    result = simulate(squad=squad, offers=[offer()], sales=["1"], buys=["20"])
    assert result.gaps >= 1
    assert (
        next(
            line for line in result.after.lines if line.position is Position.GOALKEEPER
        ).empty_slots
        == 1
    )
    assert len(squad) == 11
    assert squad[0].player.id == "1"  # input isn't changed


@pytest.mark.parametrize("timed", [False, True])
def test_only_confirmed_early_purchase_fills_forward_gap(timed):
    result = simulate(squad=full_squad()[:-1], offers=[offer(timed=timed)], buys=["20"])
    assert result.gaps == (0 if timed else 1)
    assert result.roster_size == 11
    assert result.budget == 8_000_000


def test_over_budget_buy_remains_visible_as_deficit():
    result = simulate(offers=[offer(cost=12_000_000)], buys=["20"], reserve=1_000_000)
    assert result.budget == -2_000_000
    assert result.deficit == 3_000_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sales": ["1", "1"]},
        {"buys": ["20", "20"], "offers": [offer()]},
        {"sales": ["999"]},
        {"buys": ["999"]},
        {"buys": ["1"], "offers": [offer("1")]},
        {"buys": ["20"], "offers": [offer()], "costs": {"20": 1}},
        {"buys": ["20"], "offers": [offer(bid=3_000_000)], "costs": {"20": 2_500_000}},
    ],
)
def test_invalid_or_duplicate_selections_are_not_partially_applied(kwargs):
    with pytest.raises(SimulationError):
        simulate(**kwargs)


def test_manual_price_and_reserve_credit_cannot_create_money():
    result = simulate(
        offers=[offer(bid=3_000_000)], reserve=1_000_000, buys=["20"], costs={"20": 4_000_000}
    )
    assert result.released_bids == 1_000_000
    assert result.budget_after_bids == 6_000_000


def test_absent_keeper_cannot_fill_gap_in_projected_eleven():
    injured = PlanOffer(
        replace(rec("20", Position.GOALKEEPER, start=0, status=2), price=2_000_000),
        2_000_000,
        0,
        True,
    )
    result = simulate(offers=[injured], sales=["1"], buys=["20"])
    assert result.gaps >= 1
    assert result.after.excluded[0].player.id == "20"


def test_inconsistent_budget_uses_higher_reserve_and_warns():
    reserve, warning = reserve_from(
        Budget.model_validate({"b": 10_000_000, "pbaa": 8_000_000}), 3_000_000
    )
    assert reserve == 3_000_000 and warning
    assert reserve_from(Budget.model_validate({"b": 10_000_000}), 0) == (0, None)


def test_simulation_requires_login(client):
    response = client.get("/empfehlungen/simulation", follow_redirects=False)
    assert response.status_code == 303


def test_page_can_sell_and_preserves_source_roster(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID, fixture

    before = fixture("league_squad")
    pid = before["it"][0]["i"]
    page = logged_in_client.get("/empfehlungen/simulation", params={"verkauf": pid})
    assert page.status_code == 200
    assert "Nach deinem Plan" in page.text
    assert 'name="verkauf"' in page.text
    assert "Marktwertanstiege sind nicht eingerechnet" in page.text
    assert fake_kickbase.routes[("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad")][0] == before
    assert all(r.method == "GET" or r.url.path == "/v4/user/login" for r in fake_kickbase.requests)


def test_partial_and_invalid_price(logged_in_client):
    page = logged_in_client.get("/empfehlungen/simulation", headers={"HX-Request": "true"})
    assert page.status_code == 200
    assert "<!doctype html>" not in page.text.lower()
    page = logged_in_client.get("/empfehlungen/simulation?kauf=20&preis_20=NaN")
    assert page.status_code == 200
    assert "ganze Euro" in page.text
    assert "Nach deinem Plan" not in page.text


def test_query_count_and_id_limits(logged_in_client):
    assert logged_in_client.get("/empfehlungen/simulation?verkauf=bad").status_code == 422
    assert (
        logged_in_client.get(
            "/empfehlungen/simulation", params=[("verkauf", str(i)) for i in range(1, 27)]
        ).status_code
        == 422
    )


def test_simulation_history_restore_returns_whole_page(logged_in_client):
    response = logged_in_client.get(
        "/empfehlungen/simulation",
        headers={
            "HX-Request": "true",
            "HX-History-Restore-Request": "true",
        },
    )
    assert response.status_code == 200
    assert "<html" in response.text


@pytest.mark.parametrize("before", [True, False])
def test_market_deadline_controls_projected_eleven(logged_in_client, fake_kickbase, before):
    from tests.fakes import TEAM_LEAGUE_ID
    from tests.test_buy_page import market

    data = market()
    listing = next(p for p in data["it"] if p["i"] == "10120")
    listing["exs"] = 3600 if before else 3 * 86400
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", data)
    response = logged_in_client.get("/empfehlungen/simulation?kauf=10120")
    assert response.status_code == 200
    timing_warning = "Kaufpreis eingeplant, für die kommende Elf noch nicht mitgezählt."
    assert (timing_warning in response.text) is not before


def test_private_seller_is_not_treated_as_confirmed_early_purchase(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID
    from tests.test_buy_page import market

    data = market()
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", data)
    response = logged_in_client.get("/empfehlungen/simulation?kauf=2749")
    assert response.status_code == 200
    assert "vom Verkäufer abhängig" in response.text
    assert "Kaufpreis eingeplant, für die kommende Elf noch nicht mitgezählt." in response.text


def test_manager_named_kickbase_does_not_confirm_transfer_timing(logged_in_client, fake_kickbase):
    from tests.fakes import TEAM_LEAGUE_ID
    from tests.test_buy_page import market

    data = market()
    manager = next(p for p in data["it"] if p["i"] == "2749")
    manager["u"]["n"] = "Kickbase"
    fake_kickbase.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", data)
    response = logged_in_client.get("/empfehlungen/simulation?kauf=2749")
    assert response.status_code == 200
    assert "vom Verkäufer abhängig" in response.text
    assert "Kaufpreis eingeplant, für die kommende Elf noch nicht mitgezählt." in response.text
