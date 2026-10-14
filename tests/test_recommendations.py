import copy

import pytest
from sqlalchemy import create_engine

from app.db.models import Base
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.league import ResolvedLeague
from app.kickbase.models import Position
from app.lineups.service import TeamPrediction
from app.services.recommendations import (
    BuyFilters,
    BuyView,
    Candidate,
    Recommendation,
    buy_view,
    choose_formation,
    eleven_view,
    load_basis,
    select_buys,
    sell_view,
)
from app.services.scoring import Score
from tests.fakes import (
    NOW,
    TEAM_LEAGUE_ID,
    FakeOpenLigaDb,
    add_recommendation_endpoints,
    fake_with_team_data,
    fixture,
    make_client,
    make_oldb_client,
)

LEAGUE = ResolvedLeague(TEAM_LEAGUE_ID, "Testliga", "1")


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'recs.db'}"
    sync = create_engine(url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def fake():
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    return fake


async def run(view, fake, sessions, oldb=None):
    oldb = oldb or FakeOpenLigaDb()
    async with make_client(fake) as client, make_oldb_client(oldb) as oldb_client:
        basis = await load_basis(client, oldb_client, sessions, LEAGUE, now=NOW)
        if view == "kaufen":
            return basis, await buy_view(client, sessions, LEAGUE, basis, now=NOW)
        if view == "verkaufen":
            return basis, await sell_view(client, LEAGUE, basis, now=NOW)
        return basis, await eleven_view(client, LEAGUE, basis)


def rec(pid: str, position: Position, total: float) -> Recommendation:
    player = Candidate(pid, pid, "1", None, position, 0, None)
    return Recommendation(player=player, score=Score(total, (), False), market_value=1)


def test_choose_formation_prefers_full_lineup_then_score():
    squad = (
        [rec("tw", Position.GOALKEEPER, 50)]
        + [rec(f"d{i}", Position.DEFENDER, 60 - i) for i in range(5)]
        + [rec(f"m{i}", Position.MIDFIELDER, 70 - i) for i in range(4)]
        + [rec(f"s{i}", Position.FORWARD, 80 - i) for i in range(2)]
    )
    formation, picked = choose_formation(squad)
    assert formation.label == "4-4-2"  # complete; 5-4-1 / 4-5-1 would leave slots empty
    assert [r.player.id for r in picked[Position.DEFENDER]] == ["d0", "d1", "d2", "d3"]


def test_choose_formation_with_missing_forwards():
    squad = (
        [rec("tw", Position.GOALKEEPER, 50)]
        + [rec(f"d{i}", Position.DEFENDER, 60) for i in range(3)]
        + [rec(f"m{i}", Position.MIDFIELDER, 70) for i in range(5)]
    )
    formation, _ = choose_formation(squad)
    assert formation.label in {"3-5-2", "3-6-1"}


async def test_buy_view_filters_budget_own_listings_and_min_score(fake, sessions):
    market = copy.deepcopy(fixture("league_market"))
    me = fixture("user_me")["u"]["id"]
    own = next(p for p in market["it"] if p.get("u"))
    own["u"]["i"] = me
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    _, view = await run("kaufen", fake, sessions)
    budget = fixture("league_budget")["pbaa"]
    assert view.budget == round(budget)
    assert all(r.price <= view.budget + (r.my_offer or 0) for r in view.recommendations)
    assert all(r.score.total >= view.min_score for r in view.recommendations)
    assert own["i"] not in {r.player.id for r in view.recommendations}
    ratios = [r.per_million for r in view.recommendations]
    assert ratios == sorted(ratios, reverse=True)
    assert view.over_budget > 0
    for r in view.recommendations:
        assert (
            sum(c.points for c in r.score.components) == pytest.approx(r.score.total)
            or r.score.clamped
        )


async def test_select_buys_can_include_over_budget(fake, sessions):
    _, view = await run("kaufen", fake, sessions)
    assert view.over_budget_recs  # the fixture has good offers over the budget
    assert view.over_budget == len(view.over_budget_recs)
    default, default_total = select_buys(view, BuyFilters(before_deadline=False))
    with_over, with_over_total = select_buys(
        view, BuyFilters(before_deadline=False, over_budget=True)
    )
    assert all(r.price <= view.budget + (r.my_offer or 0) for r in default)
    assert any(r.price > view.budget for r in with_over)
    assert with_over_total > default_total


async def test_existing_bid_is_not_deducted_twice(fake, sessions):
    market = copy.deepcopy(fixture("league_market"))
    listing = next(p for p in market["it"] if p["st"] == 0)
    listing["prc"] = 5_000_000
    listing["uop"] = 6_000_000
    market["it"] = [listing]
    budget = copy.deepcopy(fixture("league_budget"))
    budget["pbaa"], budget["b"] = 4_000_000, 10_000_000
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    _, view = await run("kaufen", fake, sessions)
    assert [r.player.id for r in view.recommendations] == [listing["i"]]
    assert not view.over_budget_recs
    listing["uop"] = 0
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    _, view = await run("kaufen", fake, sessions)
    assert not view.recommendations
    assert [r.player.id for r in view.over_budget_recs] == [listing["i"]]


async def test_over_budget_offers_sort_and_filter_with_the_rest(fake, sessions):
    _, view = await run("kaufen", fake, sessions)
    pos = next(iter(view.over_budget_recs)).player.position
    shown, _ = select_buys(
        view, BuyFilters(before_deadline=False, over_budget=True, sort="preis", position=pos)
    )
    assert shown and all(r.player.position == pos for r in shown)
    prices = [r.price for r in shown]
    assert prices == sorted(prices)  # over-budget offers sort in, they are not appended
    assert any(r.price > view.budget for r in shown)


async def test_no_over_budget_offers_when_budget_covers_everything(fake, sessions):
    budget = copy.deepcopy(fixture("league_budget"))
    budget["pbaa"] = 100_000_000.0
    budget["b"] = 100_000_000.0
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    _, view = await run("kaufen", fake, sessions)
    assert not view.over_budget_recs
    assert view.over_budget == 0


async def test_sell_view_flags_falling_players(fake, sessions):
    _, view = await run("verkaufen", fake, sessions)
    names = {r.player.name: r for r in view.recommendations}
    # Hansen fällt um −14 %; Maza ist nur „angeschlagen“ (kein Verkaufsgrund für sich).
    assert set(names) == {"Hansen"}
    assert names["Hansen"].reasons[0].startswith("Marktwert −")
    totals = [r.score.total for r in view.recommendations]
    assert totals == sorted(totals)
    assert view.unflagged == 8


async def test_eleven_view_excludes_injured_and_reports_changes(fake, sessions):
    squad = copy.deepcopy(fixture("league_squad"))
    injured = next(p for p in squad["it"] if p["pos"] == 3)
    injured["st"] = 1
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    _, view = await run("elf", fake, sessions)
    in_lineup = {r.player.id for line in view.lines for r in line.players}
    assert injured["i"] not in in_lineup
    assert [r.player.id for r in view.excluded] == [injured["i"]]
    assert injured["n"] in view.going_out
    assert sum(line.empty_slots for line in view.lines) == 3
    assert view.total == pytest.approx(
        sum(r.score.total for line in view.lines for r in line.players)
    )
    # C4: total_xp is the xP sum of the picked eleven, the score sum stays around for the UI.
    assert view.total_xp == pytest.approx(
        sum(r.xp.value for line in view.lines for r in line.players if r.xp is not None)
    )
    # Every lineup player carries an XP so the UI can show the breakdown.
    for line in view.lines:
        for r in line.players:
            assert r.xp is not None


async def test_basis_notes_when_sources_are_missing(fake, sessions):
    oldb = FakeOpenLigaDb()
    oldb.down = True
    basis, view = await run("kaufen", fake, sessions, oldb)
    assert basis.programmes is None
    assert any("OpenLigaDB" in note for note in basis.warnings)
    assert any("Spieltagspunkte" in note for note in basis.notes)  # empty test database
    assert all(r.score.component("programm").value == 0.5 for r in view.recommendations)


async def test_basis_without_schedule(fake, sessions):
    fake.on("GET", "/v4/competitions/1/matchdays", {"it": []})
    basis, _ = await run("verkaufen", fake, sessions)
    assert any("Kein Spielplan" in note for note in basis.notes)


# ---------------------------------------------------------------- with collected data


async def seed_db(sessions, *, points=None, values=None, positions=None):
    """points: {player_id: {matchday: points}}, values: {player_id: {day: value}}"""
    from app.db.models import MarketValue, MatchdayPoints, Player
    from tests.fakes import NOW

    schedule = fixture("competition_matchdays")
    finished = {md["day"]: md["it"] for md in schedule["it"] if all(x["st"] == 2 for x in md["it"])}
    async with sessions.begin() as session:
        for pid, pos in (positions or {}).items():
            session.add(
                Player(
                    id=pid,
                    name=pid,
                    team_id="2",
                    position=pos,
                    status=0,
                    market_value=1,
                    image=None,
                    updated_at=NOW,
                )
            )
        # every club played every finished matchday: one filler row per club keeps them complete
        for md, matches in finished.items():
            for match in matches:
                for team in (match["t1"], match["t2"]):
                    session.add(
                        MatchdayPoints(
                            season=2026,
                            matchday=md,
                            player_id=f"f{team}",
                            points=1,
                            team_id=team,
                            opponent_id=None,
                            is_home=None,
                            kickoff=NOW,
                        )
                    )
        for pid, by_day in (points or {}).items():
            for md, value in by_day.items():
                session.add(
                    MatchdayPoints(
                        season=2026,
                        matchday=md,
                        player_id=pid,
                        points=value,
                        team_id="2",
                        opponent_id="3",
                        is_home=True,
                        kickoff=NOW,
                    )
                )
        for pid, by_day in (values or {}).items():
            for day, value in by_day.items():
                session.add(MarketValue(player_id=pid, day=day, value=value))


async def test_buy_trend_uses_value_about_seven_days_ago(fake, sessions):
    from datetime import timedelta

    from app.kickbase.calendar import market_day
    from tests.fakes import NOW

    listing = next(p for p in fixture("league_market")["it"] if not p.get("u"))
    target = market_day(NOW) - timedelta(days=7)
    await seed_db(
        sessions,
        values={
            listing["i"]: {
                target - timedelta(days=1): listing["mv"] // 2,  # used: latest in window
                target - timedelta(days=3): 1,  # outside the 2-day window
            }
        },
        positions={listing["i"]: listing["pos"]},
        points={listing["i"]: {1: 50, 2: 50, 3: 50, 4: 50}},
    )
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        basis = await load_basis(client, oldb, sessions, LEAGUE, now=NOW)
        score = basis.score(
            Candidate(listing["i"], "x", listing["tid"], None, Position(listing["pos"]), 0, None),
            (listing["mv"] - listing["mv"] // 2) / (listing["mv"] // 2) * 100,
            "kaufen",
        )
        view = await buy_view(client, sessions, LEAGUE, basis, now=NOW)
    assert score.component("trend").value == 1.0  # +100 %, capped
    rec = next((r for r in view.recommendations if r.player.id == listing["i"]), None)
    if rec is not None:
        assert rec.score.component("trend").reason.startswith("Marktwert +")


async def test_form_from_points_reaches_scores_and_lineup(fake, sessions):
    squad = fixture("league_squad")["it"]
    defenders = [p for p in squad if p["pos"] == 2]
    star, weak = defenders[0]["i"], defenders[1]["i"]
    positions = {p["i"]: p["pos"] for p in squad}
    await seed_db(
        sessions,
        positions=positions,
        points={star: {1: 200, 2: 200, 3: 200, 4: 200}, weak: {4: 10}},
    )
    _, view = await run("elf", fake, sessions)
    scores = {r.player.id: r.score for line in view.lines for r in line.players}
    assert "besser als" in scores[star].component("form").reason
    assert scores[star].component("form").value > scores[weak].component("form").value
    assert scores[star].total > scores[weak].total


async def test_buy_hides_players_without_appearances(fake, sessions):
    market = fixture("league_market")["it"]
    played, idle = market[0]["i"], market[1]["i"]
    await seed_db(
        sessions,
        positions={played: market[0]["pos"], idle: market[1]["pos"]},
        points={played: {4: 80}},
    )
    _, view = await run("kaufen", fake, sessions)
    shown = {r.player.id for r in view.recommendations}
    assert idle not in shown
    assert view.without_appearances >= 1


async def test_partially_synced_matchday_is_noted(fake, sessions):
    from app.db.models import MatchdayPoints
    from tests.fakes import NOW

    await seed_db(sessions, positions={"someone": 3})
    async with sessions.begin() as session:
        await session.execute(
            MatchdayPoints.__table__.delete().where(
                MatchdayPoints.matchday == 4, MatchdayPoints.team_id == "2"
            )
        )
    basis, _ = await run("verkaufen", fake, sessions)
    assert any("Spieltag 4. ist noch nicht vollständig" in n for n in basis.notes)
    assert basis.form.matchdays[0] == 3
    assert NOW  # keeps the import used


async def test_season_end_is_not_reported_as_outage(fake, sessions):
    schedule = copy.deepcopy(fixture("competition_matchdays"))
    schedule["day"] = 35
    fake.on("GET", "/v4/competitions/1/matchdays", schedule)
    basis, _ = await run("kaufen", fake, sessions)
    assert "Saison beendet: das Programm zählt neutral." in basis.notes
    assert not any("nicht erreichbar" in n for n in basis.all_notes)


async def test_missing_next_matchday_falls_back_to_last_finished_plus_one(fake, sessions):
    schedule = copy.deepcopy(fixture("competition_matchdays"))
    schedule.pop("day")
    fake.on("GET", "/v4/competitions/1/matchdays", schedule)
    oldb = FakeOpenLigaDb()
    basis, _ = await run("kaufen", fake, sessions, oldb)
    assert basis.programmes and basis.programmes.by_team
    assert any(r.url.path.endswith("/2026/5") for r in oldb.requests)


async def test_sell_daily_threshold_is_exclusive_and_reasons_combine(fake, sessions):
    from app.db.models import LineupPrediction

    squad = copy.deepcopy(fixture("league_squad"))
    exact, both = squad["it"][0], squad["it"][1]
    exact.update(st=0, mv=99_750, sdmvt=-3_000, tfhmvt=-250)  # exactly −3.0 %: not flagged
    both.update(st=0, mv=90_000, sdmvt=10_000, tfhmvt=-10_000)  # falling and benched
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/squad", squad)
    async with sessions.begin() as session:
        session.add(
            LineupPrediction(
                source="ligainsider",
                team_id=both["tid"],
                player_id="9999",  # someone else starts
                player_name="Anderer",
                fetched_at=NOW,
            )
        )
    _, view = await run("verkaufen", fake, sessions)
    by_id = {r.player.id: r for r in view.recommendations}
    assert exact["i"] not in by_id
    assert "Marktwert −10,0 % in 24 Stunden" in by_id[both["i"]].reasons
    assert any(
        r.startswith("Startchance") and "nicht in der voraussichtlichen Startelf" in r
        for r in by_id[both["i"]].reasons
    )


def test_basis_einsatz_value():
    from app.lineups.service import InjuryNote
    from app.services.form import FormTable
    from app.services.players import StartQuote
    from app.services.recommendations import Basis
    from app.services.scoring import load_scoring_config

    basis = Basis(load_scoring_config(), FormTable({}, ()), None, {})

    def candidate(pid):
        return Candidate(pid, pid, "2", None, Position.FORWARD, 0, None)

    assert basis.einsatz_value(candidate("1")) is None  # no data at all

    basis.predicted = {"2": TeamPrediction(frozenset({"1"}))}
    assert basis.einsatz_value(candidate("1")) == pytest.approx(0.85)

    basis.predicted = {"2": TeamPrediction(frozenset({"other"}))}
    assert basis.einsatz_value(candidate("1")) == pytest.approx(0.15)

    # A name in the club's eleven couldn't be matched: maybe it's him – unknown, not 15 %.
    basis.predicted = {"2": TeamPrediction(frozenset({"other"}), ("Yuito",))}
    assert basis.einsatz_value(candidate("1")) is None

    basis.injuries = {"1": InjuryNote("verletzung", "Kreuzbandriss", None)}
    assert basis.einsatz_value(candidate("1")) == 0.0

    basis.injuries = {}
    basis.predicted = {"2": TeamPrediction(frozenset({"1"}))}
    basis.quotes = {"1": StartQuote(3, 5)}
    assert basis.einsatz_value(candidate("1")) == pytest.approx(0.75)  # 0.6·0.85 + 0.4·0.6


async def test_sell_does_not_flag_a_doubtful_starter(fake, sessions):
    from app.db.models import LineupPrediction

    # "Maza" (angeschlagen) steht in der voraussichtlichen Startelf von Team 7.
    async with sessions.begin() as session:
        session.add(
            LineupPrediction(
                source="ligainsider",
                team_id="7",
                player_id="6061",
                player_name="Maza",
                fetched_at=NOW,
            )
        )
    _, view = await run("verkaufen", fake, sessions)
    names = {r.player.name for r in view.recommendations}
    assert "Maza" not in names  # angeschlagen, aber voraussichtlich Stammspieler


async def test_sell_flags_a_benched_player_near_deadline(fake, sessions):
    from datetime import timedelta

    from app.db.models import LineupPrediction

    market = fixture("league_market")
    market["dt"] = (NOW + timedelta(hours=12)).isoformat()
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)

    # Gutiérrez (fit, Team 7) steht nicht in der voraussichtlichen Startelf → benched.
    async with sessions.begin() as session:
        session.add(
            LineupPrediction(
                source="ligainsider",
                team_id="7",
                player_id="9999",  # someone else starts
                player_name="Anderer",
                fetched_at=NOW,
            )
        )
    _, view = await run("verkaufen", fake, sessions)
    names = {r.player.name: r for r in view.recommendations}
    assert "Gutiérrez" in names  # benched → Verkaufsgrund
    assert any("nicht in der voraussichtlichen Startelf" in r for r in names["Gutiérrez"].reasons)


def test_choose_formation_fills_empty_slots_with_stand_ins():
    from dataclasses import replace

    from app.services.players import StartProbability

    fit = (
        [rec("tw", Position.GOALKEEPER, 50)]
        + [rec(f"d{i}", Position.DEFENDER, 60) for i in range(4)]
        + [rec(f"m{i}", Position.MIDFIELDER, 70) for i in range(5)]
    )  # no fit forward: every formation would leave a slot empty
    unlikely = [
        replace(rec("s-low", Position.FORWARD, 90), start=StartProbability(15, ("x",))),
        replace(rec("s-high", Position.FORWARD, 40), start=StartProbability(40, ("y",))),
        replace(rec("d-extra", Position.DEFENDER, 99), start=StartProbability(10, ("z",))),
    ]
    formation, picked = choose_formation(fit, unlikely)
    assert formation.label == "4-5-1"  # needs only one stand-in
    # The likeliest to start fills the slot, not the one with the higher score.
    assert [r.player.id for r in picked[Position.FORWARD]] == ["s-high"]
    assert "d-extra" not in {r.player.id for r in picked[Position.DEFENDER]}
    # Without stand-ins the slot stays empty.
    _, without = choose_formation(fit)
    assert without[Position.FORWARD] == []


async def test_eleven_lines_up_the_only_keeper_even_if_unlikely_to_start(fake, sessions):
    from app.db.models import Injury
    from tests.fakes import NOW

    async with sessions.begin() as session:
        session.add(
            Injury(
                source="ligainsider",
                team_id="43",
                player_id="8227",  # Vandevoordt, the only keeper of the test squad
                player_name="Vandevoordt",
                kind="aufbautraining",
                reason="Muskelverletzung",
                since=None,
                fetched_at=NOW,
            )
        )
    _, view = await run("elf", fake, sessions)
    keeper = next(line for line in view.lines if line.position == Position.GOALKEEPER)
    assert [r.player.id for r in keeper.players] == ["8227"] and keeper.empty_slots == 0
    assert [r.player.id for r in view.stand_ins] == ["8227"]
    assert view.stand_ins[0].start.percent == 20
    assert "8227" not in {r.player.id for r in view.risky}


async def test_eleven_excludes_players_out_per_ligainsider(fake, sessions):
    from app.db.models import Injury
    from tests.fakes import NOW

    async with sessions.begin() as session:
        session.add(
            Injury(
                source="ligainsider",
                team_id="43",
                player_id="8227",
                player_name="Vandevoordt",
                kind="verletzung",
                reason="Kreuzbandriss",
                since=None,
                fetched_at=NOW,
            )
        )
    _, view = await run("elf", fake, sessions)
    assert "8227" in {r.player.id for r in view.excluded}
    assert not view.stand_ins  # an injured player is no stand-in, even for an empty slot


def _page(pid, name, team_id, position, status, market_value):
    from app.services.players import PlayerPage

    return PlayerPage(
        id=pid,
        name=name,
        team_name=None,
        position=Position(position),
        status=status,
        image=None,
        market_value=market_value,
        market_value_change_7d=None,  # deliberately unusable: the box must not rely on it
        average_points=0,
        total_points=0,
        goals=0,
        assists=0,
        market_values=(),
        matchdays=(),
        upcoming=(),
        season=None,
        collected=False,
        fetched_at=NOW,
        stale=False,
        team_id=team_id,
    )


async def test_decision_agrees_with_sell_and_buy_lists(fake, sessions):
    from app.services.decision import player_decision

    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        basis = await load_basis(client, oldb, sessions, LEAGUE, now=NOW)
        sell = await sell_view(client, LEAGUE, basis, now=NOW)
        basis = await load_basis(client, oldb, sessions, LEAGUE, now=NOW)
        buy = await buy_view(client, sessions, LEAGUE, basis, now=NOW)
        flagged = {r.player.id for r in sell.recommendations}
        for r in sell.scored:
            page = _page(r.player.id, r.player.name, r.player.team_id, r.player.position, 0, 1)
            d = await player_decision(client, oldb, sessions, LEAGUE, page, NOW)
            assert d.in_squad and d.score.total == pytest.approx(r.score.total)
            assert (d.verdict == "Verkaufskandidat") == (r.player.id in flagged)
        listed = {r.player.id for r in (*buy.recommendations, *buy.over_budget_recs)}
        checked = 0
        for r in buy.scored:
            if any(s.player.id == r.player.id for s in sell.scored):
                continue  # an own player
            page = _page(
                r.player.id,
                r.player.name,
                r.player.team_id,
                r.player.position,
                r.player.status,
                r.market_value,
            )
            d = await player_decision(client, oldb, sessions, LEAGUE, page, NOW)
            assert not d.in_squad and d.score.total == pytest.approx(r.score.total)
            assert d.verdict.startswith("Kaufenswert") == (r.player.id in listed)
            checked += 1
    assert checked and flagged


async def test_decision_rejects_players_without_appearances(fake, sessions, monkeypatch):
    import app.services.decision as decision_module
    from app.services.form import FormTable

    real = decision_module.load_basis

    async def benched_basis(*args, **kwargs):
        basis = await real(*args, **kwargs)
        stats = type("S", (), {"matchdays": 3, "appearances": 0})()
        basis.form = FormTable({"777": stats}, ())
        return basis

    monkeypatch.setattr(decision_module, "load_basis", benched_basis)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        page = _page("777", "Neu", "2", 4, 0, 5_000_000)
        d = await decision_module.player_decision(client, oldb, sessions, LEAGUE, page, NOW)
    assert d.verdict == "Diese Saison ohne Einsatz – nicht empfohlen"


# ---------------------------------------------------------------- C4.3 xP/Mio.


async def test_buy_view_attaches_xp_and_xp_per_million(fake, sessions):
    """Jede Kaufempfehlung trägt xP und xP pro Mio. € (Startchance × Form × Gegner pro Preis).

    Ohne xP liefert xp_per_million None – nicht 0, sonst stünde ein Kickbase-Selbstverkauf
    (kein Preis) ganz oben in der Sortierung."""
    _, view = await run("kaufen", fake, sessions)
    assert view.recommendations, "Fixture muss Vorschläge liefern"
    for rec in view.recommendations:
        assert rec.xp is not None, f"{rec.player.name} hat kein xP"
        # Preis ist im Fixture gesetzt; das Verhältnis muss stimmen.
        assert rec.xp_per_million is not None
        assert rec.xp_per_million == pytest.approx(rec.xp.value / (rec.price / 1_000_000))


async def test_buy_view_xp_per_million_handles_missing_price(fake, sessions):
    """Ohne Preis (Kickbase-Selbstverkauf ohne `prc`) bleibt xp_per_million None statt
    zu crashen oder 0 zu werden – 0 würde die xP-Sortierung sprengen."""
    import copy

    from tests.fakes import fixture

    market = copy.deepcopy(fixture("league_market"))
    for entry in market["it"]:
        entry["prc"] = 0  # kein Preis: Division durch 0 darf nicht passieren
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/market", market)
    _, view = await run("kaufen", fake, sessions)
    assert view.recommendations
    for rec in view.recommendations:
        assert rec.xp is not None
        assert rec.xp_per_million is None, (
            f"{rec.player.name}: xp_per_million sollte None sein, ist {rec.xp_per_million}"
        )
        # per_million schützt sich ebenfalls – Konsistenz.
        assert rec.per_million is None


async def test_buy_view_xp_per_million_is_none_without_xp(fake, sessions, monkeypatch):
    """Wenn xP nicht berechnet werden kann, bleibt xp_per_million None – kein 0, der
    anschließend in der Sortierung ganz oben stünde."""
    import app.services.recommendations as rec_module

    def stub(*args, **kwargs):
        # Form/Programm unbekannt: xP ist 0,5² = 0,25 (immer noch ein Wert) – stattdessen
        # None simulieren, um den Schutz vor fehlendem xP zu prüfen.
        return None  # type: ignore[return-value]

    monkeypatch.setattr(rec_module, "expected_points", stub)
    _, view = await run("kaufen", fake, sessions)
    assert view.recommendations
    for rec in view.recommendations:
        assert rec.xp is None
        assert rec.xp_per_million is None


async def test_select_buys_sorts_by_xp_per_million(fake, sessions):
    """Die neue Sortierung `xp-pro-mio` reiht Spieler mit dem meisten xP pro Euro nach vorne.
    Tiebreaker: roh-xP, dann Score – wer ohne xP dasteht, sinkt nach unten."""
    _, view = await run("kaufen", fake, sessions)
    shown, _ = select_buys(view, BuyFilters(before_deadline=False, sort="xp-pro-mio"))
    assert shown, "Filter + Sortierung muss mindestens einen Treffer liefern"
    keys = [
        (
            -(rec.xp_per_million or 0),
            -(rec.xp.value if rec.xp else 0),
            -rec.score.total,
        )
        for rec in shown
    ]
    assert keys == sorted(keys)


async def test_select_buys_xp_per_million_picks_highest_value_first(fake, sessions):
    """Höherer xP/Mio. > niedrigerer, auch wenn der günstigere Spieler weniger Punkte
    erwarten lässt – xP/Mio. ist eine Effizienz-Kennzahl."""
    # Drei fiktive Empfehlungen mit bekannten Werten: A ist effizient, B ist teurer, C
    # ohne xP rutscht ans Ende.
    basis, _ = await run("kaufen", fake, sessions)

    from app.services.xp import XP, XPComponent

    def xp(value: float) -> XP:
        return XP(
            value=value,
            startchance=value,
            form=1.0,
            gegner=1.0,
            parts=(
                XPComponent("startchance", "Startchance", value, 1.0, value, ""),
                XPComponent("form", "Form", 1.0, 1.0, 1.0, ""),
                XPComponent("gegner", "Gegner", 1.0, 1.0, 1.0, ""),
            ),
        )

    rec_a = Recommendation(
        player=Candidate("a", "A", "1", "Club", Position.FORWARD, 0, None),
        score=Score(70, (), False),
        market_value=5_000_000,
        price=2_000_000,
        xp=xp(0.30),
        xp_per_million=0.30 / 2,
        expires_at=None,
        before_deadline=None,
    )
    rec_b = Recommendation(
        player=Candidate("b", "B", "1", "Club", Position.FORWARD, 0, None),
        score=Score(85, (), False),
        market_value=10_000_000,
        price=10_000_000,
        xp=xp(0.50),
        xp_per_million=0.50 / 10,
        expires_at=None,
        before_deadline=None,
    )
    rec_c = Recommendation(
        player=Candidate("c", "C", "1", "Club", Position.FORWARD, 0, None),
        score=Score(95, (), False),
        market_value=20_000_000,
        price=20_000_000,
        xp=None,
        xp_per_million=None,
        expires_at=None,
        before_deadline=None,
    )
    view = BuyView(
        budget=50_000_000,
        recommendations=(rec_a, rec_b, rec_c),
        over_budget=0,
        below_min_score=0,
        without_appearances=0,
        min_score=0,
        profile=basis.config.profiles["kaufen"],
    )
    shown, _ = select_buys(view, BuyFilters(sort="xp-pro-mio", before_deadline=False))
    assert [r.player.id for r in shown] == ["a", "b", "c"], (
        "A (höchstes xP/Mio.) zuerst, B danach, C (kein xP) zuletzt."
    )
