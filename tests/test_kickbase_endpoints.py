"""Every typed client method against the recorded (anonymised) fixture of its endpoint."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.kickbase import models as m
from tests.fakes import FakeKickbase, fixture, make_client

L, C, P, T = "1000002", "1", "6292", "29"

ENDPOINTS = [
    ("user_me", (), "/v4/user/me", "user_me", m.UserMe),
    ("leagues", (), "/v4/leagues/selection", "leagues_selection", m.LeagueSelection),
    ("league_me", (L,), f"/v4/leagues/{L}/me", "league_me", m.LeagueMe),
    ("budget", (L,), f"/v4/leagues/{L}/me/budget", "league_budget", m.Budget),
    ("squad", (L,), f"/v4/leagues/{L}/squad", "league_squad", m.Squad),
    ("lineup", (L,), f"/v4/leagues/{L}/lineup", "league_lineup", m.Lineup),
    ("my_eleven", (L,), f"/v4/leagues/{L}/teamcenter/myeleven", "league_myeleven", m.MyEleven),
    ("ranking", (L,), f"/v4/leagues/{L}/ranking", "league_ranking", m.Ranking),
    ("market", (L,), f"/v4/leagues/{L}/market", "league_market", m.Market),
    (
        "transfers",
        (L,),
        f"/v4/leagues/{L}/activitiesFeed",
        "league_activities_transfers",
        m.ActivityFeed,
    ),
    ("league_player", (L, P), f"/v4/leagues/{L}/players/{P}", "league_player", m.PlayerDetails),
    (
        "league_player_performance",
        (L, P),
        f"/v4/leagues/{L}/players/{P}/performance",
        "league_player_performance",
        m.Performance,
    ),
    (
        "league_player_market_value",
        (L, P),
        f"/v4/leagues/{L}/players/{P}/marketValue/92",
        "league_player_market_value_92",
        m.MarketValueHistory,
    ),
    ("competitions", (), "/v4/competitions", "competitions", m.Competitions),
    (
        "competition_players",
        (C,),
        f"/v4/competitions/{C}/players",
        "competition_players",
        m.CompetitionPlayers,
    ),
    (
        "competition_table",
        (C,),
        f"/v4/competitions/{C}/table",
        "competition_table",
        m.CompetitionTable,
    ),
    (
        "competition_matchdays",
        (C,),
        f"/v4/competitions/{C}/matchdays",
        "competition_matchdays",
        m.Matchdays,
    ),
    (
        "competition_player",
        (C, P),
        f"/v4/competitions/{C}/players/{P}",
        "competition_player",
        m.PlayerDetails,
    ),
    (
        "competition_player_performance",
        (C, P),
        f"/v4/competitions/{C}/players/{P}/performance",
        "competition_player_performance",
        m.Performance,
    ),
    (
        "competition_player_market_value",
        (C, P),
        f"/v4/competitions/{C}/players/{P}/marketValue/92",
        "competition_player_market_value_92",
        m.MarketValueHistory,
    ),
    (
        "team_profile",
        (C, T),
        f"/v4/competitions/{C}/teams/{T}/teamprofile",
        "competition_team_profile",
        m.TeamProfile,
    ),
    (
        "team_center",
        (C, T, 4),
        f"/v4/competitions/{C}/teams/{T}/teamcenter",
        "competition_team_center",
        m.TeamCenter,
    ),
    (
        "user_teamcenter",
        (L, "9000001", 5),
        f"/v4/leagues/{L}/users/9000001/teamcenter",
        "league_user_teamcenter",
        m.UserTeamCenter,
    ),
    ("match_details", ("11914",), "/v4/matches/11914/details", "match_details", m.MatchDetails),
]


@pytest.mark.parametrize(
    ("method", "args", "path", "fixture_name", "model"),
    ENDPOINTS,
    ids=[e[0] for e in ENDPOINTS],
)
async def test_typed_endpoint(method, args, path, fixture_name, model):
    fake = FakeKickbase()
    fake.on("GET", path, fixture(fixture_name))
    async with make_client(fake) as client:
        result = await getattr(client, method)(*args)
    assert isinstance(result.data, model)
    assert [r.url.path for r in fake.gets()] == [path]


async def test_ranking_passes_matchday_parameter():
    fake = FakeKickbase()
    fake.on("GET", f"/v4/leagues/{L}/ranking", fixture("league_ranking"))
    async with make_client(fake) as client:
        await client.ranking(L, matchday=3)
    assert fake.gets()[0].url.params["dayNumber"] == "3"


async def test_invalid_market_value_timeframe():
    async with make_client(FakeKickbase()) as client:
        with pytest.raises(ValueError):
            await client.league_player_market_value(L, P, timeframe=30)


def test_squad_player_fields():
    player = m.Squad.model_validate(fixture("league_squad")).players[0]
    assert player.position is m.Position.DEFENDER
    assert player.position.short_label == "ABW"
    assert player.market_value == 3722776
    assert player.market_value_trend is m.MarketValueTrend.FALLING
    assert player.market_value_change_7d < 0
    assert player.status_label == "fit"


def test_budget_after_offers():
    budget = m.Budget.model_validate(fixture("league_budget"))
    assert budget.budget_after_offers < budget.budget


def test_budget_without_open_offers():
    # Kickbase leaves out "pbaa" when there are no open offers.
    raw = fixture("league_budget")
    raw.pop("pbaa")
    budget = m.Budget.model_validate(raw)
    assert budget.budget_after_offers == budget.budget


def test_market_sellers_and_expiry():
    market = m.Market.model_validate(fixture("league_market"))
    sold_by_managers = [p for p in market.players if p.seller]
    assert sold_by_managers, "fixture should contain listings by managers"
    assert all(p.seller.name.startswith("Manager ") for p in sold_by_managers)
    assert all(p.expires_in_seconds is not None for p in market.players if not p.seller)


def test_ranking_lookup():
    ranking = m.Ranking.model_validate(fixture("league_ranking"))
    first = ranking.users[0]
    assert ranking.for_user(first.id) == first
    assert ranking.for_user("missing") is None


def test_performance_parsing():
    season = m.Performance.model_validate(fixture("league_player_performance")).seasons[-1]
    played = [e for e in season.matchdays if e.points is not None]
    assert played and all(isinstance(e.minutes_played, int) for e in played)
    assert played[0].opponent_id is not None
    future = [e for e in season.matchdays if e.points is None]
    assert all(e.minutes_played is None for e in future)


@pytest.mark.parametrize(
    ("raw", "minutes"), [("76'", 76), ("90+3'", 90), ("", None), (None, None), ("-", None)]
)
def test_minutes_parsing(raw, minutes):
    entry = m.PerformanceEntry.model_validate({"day": 1, "mp": raw})
    assert entry.minutes_played == minutes


def test_market_value_days_are_dates():
    history = m.MarketValueHistory.model_validate(fixture("league_player_market_value_92"))
    assert len(history.points) == 92
    assert history.points[0].day < history.points[-1].day
    assert m.MarketValuePoint(dt=0, mv=1).day == date(1970, 1, 1)


def test_unknown_status_label():
    assert m.status_label(1) == "verletzt"
    assert m.status_label(999) == "Status 999"


def test_every_public_endpoint_is_covered_by_a_fixture_test():
    import inspect

    from app.kickbase.client import KickbaseClient

    generic = {"get_json", "aclose"}
    public = {
        name
        for name, member in inspect.getmembers(KickbaseClient, inspect.iscoroutinefunction)
        if not name.startswith("_") and name not in generic
    }
    assert public == {e[0] for e in ENDPOINTS}


def test_unowned_player_has_no_owner():
    assert m.PlayerDetails.model_validate(fixture("competition_player")).owner_id is None
    owned = m.PlayerDetails.model_validate(fixture("league_player"))
    assert owned.owner_id is not None and owned.owner_id != "0"


def test_money_with_fraction_does_not_fail_the_list():
    history = m.MarketValueHistory.model_validate({"it": [{"dt": 1, "mv": 1.6}], "lmv": 2.4})
    assert history.points[0].value == 2
    assert history.lowest == 2


def test_unknown_market_value_trend_is_tolerated():
    data = fixture("league_squad")
    data["it"][0]["mvt"] = 7
    squad = m.Squad.model_validate(data)
    assert squad.players[0].market_value_trend is m.MarketValueTrend.UNKNOWN


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, m.Availability.FIT),
        (2, m.Availability.DOUBTFUL),
        (4, m.Availability.REHAB),
        (1, m.Availability.OUT),
        (8, m.Availability.OUT),
        (12345, m.Availability.UNKNOWN),
    ],
)
def test_availability(code, expected):
    assert m.availability(code) is expected


def test_finished_matches():
    schedule = m.Matchdays.model_validate(fixture("competition_matchdays"))
    first = schedule.matchdays[0].matches[0]
    last = schedule.matchdays[-1].matches[0]
    assert first.is_finished and not last.is_finished
    assert schedule.next_matchday == 5
    season = m.Performance.model_validate(fixture("league_player_performance")).seasons[-1]
    assert [e.matchday for e in season.matchdays if e.is_finished] == [1, 2, 3, 4]


def test_market_expiry_is_relative_to_fetch_time():
    player = next(
        p for p in m.Market.model_validate(fixture("league_market")).players if p.expires_in_seconds
    )
    fetched_at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert player.expires_at(fetched_at) == fetched_at + timedelta(
        seconds=player.expires_in_seconds
    )


def test_datetimes_without_offset_are_utc():
    match = m.Match.model_validate(
        {"mi": "1", "day": 1, "dt": "2026-10-10T13:30:00", "t1": "2", "t2": "3"}
    )
    assert match.kickoff.tzinfo is not None
    assert match.kickoff.utcoffset() == timedelta(0)


def test_team_center_points_and_match():
    center = m.TeamCenter.model_validate(fixture("competition_team_center"))
    assert center.match is not None and center.match.is_finished
    assert len(center.players) == 16
    assert all(isinstance(p.points, int) for p in center.players)


async def test_transfers_request_only_transfer_activities():
    fake = FakeKickbase()
    path = f"/v4/leagues/{L}/activitiesFeed"
    fake.on("GET", path, fixture("league_activities_transfers"))
    async with make_client(fake) as client:
        feed = (await client.transfers(L, start=50, limit=50)).data
    [request] = fake.gets()
    assert dict(request.url.params) == {"start": "50", "max": "50", "filter": "15"}
    transfers = [a.transfer() for a in feed.items]
    assert all(t is not None for t in transfers)
    assert {t.kind for t in transfers} == {1, 2}
    bought = [t for t in transfers if t.kind == m.TransferKind.BOUGHT]
    assert all(t.buyer and t.buyer.startswith("Manager ") for t in bought)  # anonymised
    assert all(t.buyer is None for t in transfers if t.kind == m.TransferKind.SOLD_TO_KICKBASE)


def test_market_deadline():
    market = m.Market.model_validate(fixture("league_market"))
    assert market.deadline == datetime(2026, 10, 9, 18, 30, tzinfo=UTC)  # Friday 20:30 CEST
