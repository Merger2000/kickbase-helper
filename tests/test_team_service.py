import httpx
import pytest

from app.kickbase.errors import KickbaseUnavailable
from app.kickbase.league import ResolvedLeague
from app.kickbase.models import Availability, Position
from app.services.team import image_url, load_team
from tests.fakes import TEAM_LEAGUE_ID, fake_with_team_data, fixture, make_client

LEAGUE = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")


async def load(fake=None):
    fake = fake or fake_with_team_data()
    async with make_client(fake) as client:
        return await load_team(client, LEAGUE)


async def test_header_figures():
    team = await load()
    squad = fixture("league_squad")["it"]
    assert team.budget == 17012409
    assert team.budget_after_offers < team.budget
    assert team.team_value == sum(round(p["mv"]) for p in squad) == team.squad_value
    assert team.manager_count == 6
    assert team.placement is not None


async def test_pitch_lines_and_open_slots():
    team = await load()
    assert team.formation.label == "3-5-2"
    assert [line.position for line in team.lines] == [
        Position.FORWARD,
        Position.MIDFIELDER,
        Position.DEFENDER,
        Position.GOALKEEPER,
    ]
    assert [len(line.players) for line in team.lines] == [0, 5, 3, 1]
    assert team.lines[0].empty_slots == 2
    assert team.open_slots == 2
    slots = [p.lineup_slot for p in team.lines[1].players]
    assert slots == sorted(slots)


async def test_players_missing_from_my_eleven_are_on_the_bench():
    fake = fake_with_team_data()
    eleven = fixture("league_myeleven")
    benched = eleven["lp"].pop()
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven", eleven)
    team = await load(fake)
    assert [p.id for p in team.bench] == [benched["i"]]
    assert sum(len(line.players) for line in team.lines) == 8


async def test_squad_groups_sorted_by_market_value():
    team = await load()
    assert [g.position for g in team.squad] == [
        Position.GOALKEEPER,
        Position.DEFENDER,
        Position.MIDFIELDER,
    ]
    for group in team.squad:
        values = [p.market_value for p in group.players]
        assert values == sorted(values, reverse=True)


async def test_player_card_details():
    team = await load()
    cards = {p.name: p for g in team.squad for p in g.players}
    hansen = cards["Hansen"]
    assert hansen.team_name == "Paderborn"
    assert hansen.recent_points == (64, 52, 44, 93)  # oldest first
    assert hansen.market_value_change_7d_percent == pytest.approx(-12.57, abs=0.01)
    assert hansen.image.startswith("https://kickbase.b-cdn.net/content/file/")
    assert cards["Maza"].availability is Availability.DOUBTFUL
    regeer = cards["Regeer"]
    assert regeer.recent_points[0] is None  # did not play


async def test_kickbase_errors_propagate():
    fake = fake_with_team_data()
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
    with pytest.raises(KickbaseUnavailable):
        await load(fake)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (None, None),
        ("", None),
        ("content/file/a.png", "https://kickbase.b-cdn.net/content/file/a.png"),
        ("https://kickbase.b-cdn.net/x.png", "https://kickbase.b-cdn.net/x.png"),
    ],
)
def test_image_url(path, expected):
    assert image_url(path) == expected


LIVE_PATHS = [
    f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/ranking",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/squad",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/lineup",
    f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven",
]


async def test_refresh_refetches_live_data_only():
    from tests.fakes import FakeClock

    fake = fake_with_team_data()
    clock = FakeClock()
    async with make_client(fake, clock=clock) as client:
        await load_team(client, LEAGUE)
        clock.advance(seconds=61)
        await load_team(client, LEAGUE, refresh=True)
    for path in LIVE_PATHS:
        assert len(fake.gets(path)) == 2, path
    assert len(fake.gets("/v4/user/me")) == 1
    assert len(fake.gets("/v4/competitions/1/table")) == 1


async def test_stale_data_is_flagged_with_its_age():
    from tests.fakes import NOW, FakeClock

    fake = fake_with_team_data()
    clock = FakeClock()
    async with make_client(fake, clock=clock) as client:
        await load_team(client, LEAGUE)
        clock.advance(minutes=20)
        fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", httpx.Response(503))
        team = await load_team(client, LEAGUE)
    assert team.stale is True
    assert team.fetched_at == NOW


async def test_freshness_ignores_long_cached_profile():
    from tests.fakes import FakeClock

    fake = fake_with_team_data()
    clock = FakeClock()
    async with make_client(fake, clock=clock) as client:
        await load_team(client, LEAGUE)
        clock.advance(minutes=11)  # live data expired, profile (24 h) and table (30 min) not
        team = await load_team(client, LEAGUE)
    assert team.fetched_at == clock.now


async def test_lined_up_player_missing_from_squad_is_still_drawn(caplog):
    fake = fake_with_team_data()
    squad = fixture("league_squad")
    removed = next(p for p in squad["it"] if p["pos"] == 2)
    squad["it"].remove(removed)
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    team = await load(fake)
    defenders = next(line for line in team.lines if line.position is Position.DEFENDER)
    assert removed["i"] in [p.id for p in defenders.players]
    assert defenders.empty_slots == 0
    assert team.open_slots == 2
    assert "fehlen im Kader" in caplog.text


async def test_missing_goalkeeper_is_an_open_slot():
    fake = fake_with_team_data()
    eleven = fixture("league_myeleven")
    eleven["lp"] = [p for p in eleven["lp"] if p["pos"] != 1]
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven", eleven)
    team = await load(fake)
    assert team.lines[-1].position is Position.GOALKEEPER
    assert team.lines[-1].empty_slots == 1
    assert team.open_slots == 3


async def test_empty_lineup_shows_eleven_open_slots():
    fake = fake_with_team_data()
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven", {"lp": [], "nlp": []})
    team = await load(fake)
    assert team.open_slots == 11
    assert len(team.bench) == 9


async def test_bench_sorted_by_position_then_value():
    fake = fake_with_team_data()
    eleven = fixture("league_myeleven")
    eleven["lp"] = eleven["lp"][:5]
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/teamcenter/myeleven", eleven)
    team = await load(fake)
    keys = [(p.position.value, -p.market_value) for p in team.bench]
    assert keys == sorted(keys)


async def test_missing_from_ranking_logs_and_has_no_placement(caplog):
    fake = fake_with_team_data()
    me = fixture("user_me")
    me["u"]["id"] = "not-in-ranking"
    fake.on("GET", "/v4/user/me", me)
    team = await load(fake)
    assert team.placement is None
    assert "fehlt im Ranking" in caplog.text
