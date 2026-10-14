import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from app.db.models import Base, MarketValue, MatchdayPoints, MatchLineup, Player, Team
from app.db.session import make_engine, make_sessionmaker
from app.kickbase import models as m
from app.kickbase.calendar import market_day
from app.kickbase.league import ResolvedLeague
from app.lineups.service import InjuryNote, Prognosis
from app.services.players import StartQuote, compute_start_probability, load_player, search_players
from tests.fakes import NOW, fixture, make_client
from tests.test_jobs import bundesliga_fake

LEAGUE = ResolvedLeague("1000002", "Testliga", "1")
PLAYER = fixture("competition_player")
PID = PLAYER["i"]
TEAM = PLAYER["tid"]
LATEST = market_day(NOW)


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'players.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    sync = create_engine(url)
    Base.metadata.drop_all(sync)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def player_fake():
    fake = bundesliga_fake()
    fake.on("GET", f"/v4/competitions/1/players/{PID}", PLAYER)
    fake.on(
        "GET",
        f"/v4/competitions/1/players/{PID}/marketValue/92",
        fixture("competition_player_market_value_92"),
    )
    fake.on(
        "GET",
        f"/v4/competitions/1/players/{PID}/performance",
        fixture("competition_player_performance"),
    )
    return fake


async def seed(sessions, *, values=40, last_day=LATEST, matchdays=(1, 2, 4), club_days=(3,)):
    async with sessions.begin() as session:
        session.add(Team(id=TEAM, name="Paderborn", image=None, updated_at=NOW))
        session.add(
            Player(
                id=PID,
                name=PLAYER["ln"],
                team_id=TEAM,
                position=PLAYER["pos"],
                status=0,
                market_value=PLAYER["mv"],
                image=None,
                updated_at=NOW,
            )
        )
        for i in range(values):
            session.add(
                MarketValue(player_id=PID, day=last_day - timedelta(days=i), value=1_000_000 + i)
            )
        for md in matchdays:
            session.add(
                MatchdayPoints(
                    season=2026,
                    matchday=md,
                    player_id=PID,
                    points=10 * md,
                    team_id=TEAM,
                    opponent_id="2",
                    is_home=md % 2 == 0,
                    kickoff=NOW,
                )
            )
        for md in club_days:  # a teammate's row: the club's matchday is synced
            session.add(
                MatchdayPoints(
                    season=2026,
                    matchday=md,
                    player_id="teammate",
                    points=5,
                    team_id=TEAM,
                    opponent_id="2",
                    is_home=True,
                    kickoff=NOW,
                )
            )


async def test_player_page_from_collected_data(sessions):
    await seed(sessions)
    fake = player_fake()
    async with make_client(fake) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert page.collected
    assert len(page.market_values) == 40
    assert [b.matchday for b in page.matchdays] == [1, 2, 3, 4]
    assert [b.played for b in page.matchdays] == [True, True, False, True]
    assert page.matchdays[0].opponent == "Bayern"
    assert page.matches_played == 3
    assert not fake.gets(f"/v4/competitions/1/players/{PID}/marketValue/92")
    assert not fake.gets(f"/v4/competitions/1/players/{PID}/performance")
    assert page.chart_data["points"] == [10, 20, 0, 40]


async def test_unsynced_matchdays_come_from_kickbase_not_as_benched(sessions):
    await seed(sessions, club_days=())  # matchday 3 not synced for the club
    fake = player_fake()
    async with make_client(fake) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    md3 = next(b for b in page.matchdays if b.matchday == 3)
    perf = fixture("competition_player_performance")["it"][-1]["ph"]
    assert md3.played and md3.points == next(e["p"] for e in perf if e["day"] == 3)
    assert not page.collected


async def test_player_page_falls_back_to_live_data(sessions):
    fake = player_fake()
    async with make_client(fake) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert not page.collected
    assert len(page.market_values) == 92
    perf = fixture("competition_player_performance")["it"][-1]["ph"]
    expected = [(e["day"], e.get("p", 0), "p" in e) for e in perf if e.get("mdst") == 2]
    assert [(b.matchday, b.points, b.played) for b in page.matchdays] == expected
    assert all(b.opponent for b in page.matchdays if b.played)


@pytest.mark.parametrize(
    ("values", "last_day"),
    [(10, LATEST), (40, LATEST - timedelta(days=5))],  # too short, too old
)
async def test_thin_or_outdated_series_uses_live_history(sessions, values, last_day):
    await seed(sessions, values=values, last_day=last_day)
    fake = player_fake()
    async with make_client(fake) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert len(page.market_values) == 92
    assert fake.gets(f"/v4/competitions/1/players/{PID}/marketValue/92")


async def test_market_value_change_over_seven_days(sessions):
    await seed(sessions)
    async with make_client(player_fake()) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert page.market_value_change_7d == PLAYER["mv"] - (1_000_000 + 7)


async def test_player_page_shows_start_quote(sessions):
    await seed(sessions)
    # 7 appearances inserted in non-chronological order: only the 5 youngest count.
    started = {0: True, 1: False, 2: True, 3: True, 4: False, 5: True, 6: False}
    async with sessions.begin() as session:
        for i in range(7):
            session.add(
                MatchLineup(
                    match_id=f"m{i}",
                    player_id=PID,
                    season=2026,
                    matchday=i + 1,
                    team_id=TEAM,
                    started=started[i],
                    kickoff=NOW - timedelta(days=7 - i),
                )
            )
    async with make_client(player_fake()) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    # The 5 youngest (i=2..6): True, True, False, True, False → 3 started of 5.
    assert page.start_quote is not None
    assert (page.start_quote.started, page.start_quote.total) == (3, 5)


async def test_player_page_without_lineups_has_no_start_quote(sessions):
    await seed(sessions)
    async with make_client(player_fake()) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert page.start_quote is None


def test_change_needs_a_value_near_seven_days_ago():
    from app.services.players import _change_since

    day = LATEST
    assert _change_since([(day - timedelta(days=30), 1), (day, 5)], 5, day) is None
    assert _change_since([(day - timedelta(days=8), 1), (day, 5)], 5, day) == 4


async def test_upcoming_matches_of_the_players_club(sessions):
    async with make_client(player_fake()) as client:
        page = await load_player(client, sessions, LEAGUE, PID)
    assert 1 <= len(page.upcoming) <= 3
    kickoffs = [m.kickoff for m in page.upcoming]
    assert kickoffs == sorted(kickoffs)
    assert all(m.opponent for m in page.upcoming)


def player(id, name, team_id, position, value, *, status=0, image=None, updated=NOW):
    return Player(
        id=id,
        name=name,
        team_id=team_id,
        position=position,
        status=status,
        market_value=value,
        image=image,
        updated_at=updated,
    )


async def test_search_players(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                Team(id="1", name="Bayern", image=None, updated_at=NOW),
                Team(id="2", name="Mainz", image=None, updated_at=NOW),
                player("1", "Müller", "1", 3, 5_000_000),
                player("2", "Kimmich", "1", 2, 30_000_000, status=1, image="content/file/x.png"),
                player("3", "Sano", "2", 3, 20_000_000),
                player("4", "Ohne Verein", "99", 4, 1_000_000),
                player("5", "Weg", "2", 3, 50_000_000, updated=NOW - timedelta(days=3)),
            ]
        )
    result = await search_players(sessions)
    assert [p.name for p in result.players] == ["Kimmich", "Sano", "Müller", "Ohne Verein"]
    assert result.as_of == NOW
    assert [p.name for p in (await search_players(sessions, "bayern")).players] == [
        "Kimmich",
        "Müller",
    ]
    assert [p.name for p in (await search_players(sessions, "mül")).players] == ["Müller"]
    assert [p.name for p in (await search_players(sessions, position=3)).players] == [
        "Sano",
        "Müller",
    ]
    assert (await search_players(sessions, "K%h")).players == []  # no wildcards
    assert result.players[0].image.startswith("https://kickbase.b-cdn.net/")
    assert result.players[-1].team_name is None


async def test_search_before_first_sync(sessions):
    result = await search_players(sessions, "x")
    assert result.players == [] and result.as_of is None


IN, NOT, NONE = Prognosis(True), Prognosis(False), Prognosis()
BATTLE = Prognosis(None, battle=True)
FIT = m.Availability.FIT


def chance(prognosis, availability=FIT, quote=None, injury=None):
    return compute_start_probability(prognosis, injury, availability, quote)


def test_start_probability_out_is_zero():
    p = chance(IN, injury=InjuryNote("verletzung", "Kreuzbandriss", None), quote=StartQuote(3, 5))
    assert p.percent == 0 and p.reasons == ("Verletzt: Kreuzbandriss",)
    for kind in ("verletzung", "gesperrt", "krankheit", "nicht_im_kader"):
        assert chance(IN, injury=InjuryNote(kind, None, None)).percent == 0, kind
    assert chance(IN, m.Availability.OUT).percent == 0


def test_start_probability_prognosis_and_quote():
    assert chance(IN).percent == 85
    assert chance(BATTLE).percent == 50
    assert chance(NOT).percent == 15
    assert chance(IN, quote=StartQuote(3, 5)).percent == 75  # 0.6·85 + 0.4·60
    assert chance(NOT, quote=StartQuote(3, 5)).percent == 33  # 0.6·15 + 0.4·60
    p = chance(IN, quote=StartQuote(1, 2))
    assert p.percent == 85 and not any("Startquote" in r for r in p.reasons)  # too few


def test_start_probability_without_or_with_unclear_prognosis():
    p = chance(NONE)
    assert p.percent is None and p.reasons == ("keine Prognose vorhanden",)
    p = chance(NONE, quote=StartQuote(3, 5))
    assert p.percent == 60 and p.reasons == ("keine Prognose, Startquote 3/5",)
    unclear = Prognosis(None, unmatched=("Yuito",))
    assert chance(unclear).percent is None
    p = chance(unclear, quote=StartQuote(4, 5))
    assert p.percent == 80
    assert p.reasons == ("Prognose unklar (Yuito nicht zugeordnet), Startquote 4/5",)


def test_start_probability_caps_instead_of_overrides():
    doubtful, rehab = m.Availability.DOUBTFUL, m.Availability.REHAB
    assert chance(IN, doubtful).percent == 50
    assert chance(IN, doubtful, StartQuote(5, 5)).percent == 50
    assert chance(IN, rehab).percent == 20
    assert chance(NONE, doubtful).percent == 50  # nothing else known: the cap
    assert chance(NONE, rehab).percent == 20
    p = chance(IN, rehab)
    assert p.reasons[0] == "im Aufbautraining"


def test_start_probability_is_monotonic():
    """A limited player never ranks above a fit one in the same situation (review finding)."""
    quotes = (None, StartQuote(0, 5), StartQuote(3, 5), StartQuote(5, 5))
    for prognosis in (IN, BATTLE, NOT, NONE):
        for quote in quotes:
            fit = chance(prognosis, FIT, quote).percent
            for limited in (m.Availability.DOUBTFUL, m.Availability.REHAB):
                capped = chance(prognosis, limited, quote).percent
                if fit is not None and capped is not None:
                    assert capped <= fit, (prognosis, quote, limited)
    # Fit, not predicted, 0/5 vs doubtful not predicted 0/5: both 9 %, not 50 % for the doubtful.
    assert chance(NOT, m.Availability.DOUBTFUL, StartQuote(0, 5)).percent == 9


def test_start_probability_ligainsider_rehab_and_unknown_entries():
    rehab = InjuryNote("aufbautraining", "nach Muskelfaserriss", None)
    p = chance(IN, injury=rehab)
    assert p.percent == 20 and p.reasons[0] == "im Aufbautraining: nach Muskelfaserriss"
    assert chance(NOT, injury=rehab).percent == 15  # capped, never raised
    unknown = InjuryNote("unbekannt", None, None)
    p = chance(IN, injury=unknown)
    assert p.percent == 50 and "LigaInsider" in p.reasons[0]


async def test_load_market_listing_returns_price_and_bid(sessions):
    from app.db.models import LeagueTransfer
    from app.services.players import load_market_listing
    from tests.fakes import FakeKickbase

    async with sessions.begin() as session:
        session.add(
            LeagueTransfer(
                activity_id="a1",
                league_id="1000002",
                at=NOW - timedelta(days=1),
                player_id="1",
                player_name="X",
                team_id=None,
                kind=1,
                price=2_200_000,
                by_me=False,
                market_value=2_000_000,
            )
        )
    fake = FakeKickbase()
    fake.on("GET", "/v4/user/me", fixture("user_me"))
    fake.on("GET", "/v4/leagues/1000002/market", fixture("league_market"))
    async with make_client(fake) as client:
        listing = await load_market_listing(client, sessions, LEAGUE, "577", NOW)
    assert listing is not None and listing.price == 500000
    assert listing.bid_hint is not None  # one transfer is not enough for a bid recommendation
    assert not listing.stale
    async with make_client(fake) as client:
        assert await load_market_listing(client, sessions, LEAGUE, "gibtsnicht", NOW) is None


async def test_market_listing_ignores_own_and_ended_offers(sessions):
    import copy

    from app.services.players import load_market_listing
    from tests.fakes import FakeKickbase

    market = copy.deepcopy(fixture("league_market"))
    me = fixture("user_me")["u"]
    own = next(p for p in market["it"] if p.get("u"))
    own["u"] = {"i": me["id"], "n": me["name"]}
    fake = FakeKickbase()
    fake.on("GET", "/v4/user/me", fixture("user_me"))
    fake.on("GET", "/v4/leagues/1000002/market", market)
    async with make_client(fake) as client:
        # Own listing: no "Auf dem Markt" card with a bid on the own player.
        assert await load_market_listing(client, sessions, LEAGUE, own["i"], NOW) is None
        # A Kickbase offer that ended (e.g. served from a stale cache) is not shown.
        later = NOW + timedelta(days=2)
        assert await load_market_listing(client, sessions, LEAGUE, "577", later) is None


def test_start_quote_counts_club_matches_without_the_player():
    from datetime import UTC, datetime

    from app.services.players import _quotes

    def at(day):
        return datetime(2026, 9, day, 15, 30, tzinfo=UTC)

    rows = []
    # Club "2" played 6 matches; the regular started the first 3 and wasn't in the squad since.
    for i, day in enumerate((1, 8, 15, 22, 29, 30)):
        rows.append(("other", f"m{i}", "2", at(day), True))
        if i < 3:
            rows.append(("regular", f"m{i}", "2", at(day), True))
    # A newcomer since match m4: started both matches he could play.
    rows += [("new", "m4", "2", at(29), True), ("new", "m5", "2", at(30), True)]
    quotes = _quotes(rows)
    # Last 5 club matches m1–m5: started m1, m2 → 2/5 (before: 3/3, his own last 5 rows).
    assert quotes["regular"] == StartQuote(2, 5)
    assert quotes["new"] == StartQuote(2, 2)  # matches before he joined don't count
    assert quotes["other"] == StartQuote(5, 5)


def test_start_quote_after_a_move_within_the_league():
    from datetime import UTC, datetime

    from app.services.players import _quotes

    rows = [
        ("p", "a1", "old", datetime(2026, 9, 1, tzinfo=UTC), True),
        ("x", "b1", "new", datetime(2026, 9, 8, tzinfo=UTC), True),
        ("p", "b2", "new", datetime(2026, 9, 15, tzinfo=UTC), False),
        ("x", "b2", "new", datetime(2026, 9, 15, tzinfo=UTC), True),
    ]
    # Only the new club counts, from his first appearance there.
    assert _quotes(rows)["p"] == StartQuote(0, 1)
