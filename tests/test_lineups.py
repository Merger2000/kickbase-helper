import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db.models import Base, LineupPrediction, Player, Team
from app.db.session import make_engine, make_sessionmaker
from app.lineups.ligainsider import (
    DomainPacer,
    FormatError,
    Injury,
    LigaInsiderClient,
    LineupPlayer,
    TeamInjuries,
    load_config,
    parse_injuries,
    parse_lineup,
)
from app.lineups.service import (
    LINEUPS_STATUS_KEY,
    InjuryNote,
    Prognosis,
    TeamPrediction,
    injury_for_player,
    ligainsider_hint,
    predicted_players,
    prediction_for,
    store_injuries,
    store_lineup,
)
from app.news.matching import KnownPlayer
from app.worker.jobs import collect_injuries, collect_lineup_predictions
from tests.fakes import no_sleep

FIXTURE = Path(__file__).parent / "fixtures" / "ligainsider" / "aufstellung.html"
VERLETZTE = Path(__file__).parent / "fixtures" / "ligainsider" / "verletzte.html"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'lineups.db'}"
    else:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("TEST_POSTGRES_URL not set")
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def test_config_maps_all_18_teams():
    config = load_config()
    assert len(config.teams) == 18
    # Some crest images use a different slug than their club's page URL.
    assert config.team_by_slug["tsg-1899-hoffenheim"] == "14"
    assert config.team_by_slug["fsv-mainz-05"] == "18"
    assert config.team_by_slug["1fc-union-berlin"] == "40"
    assert config.team_by_slug["fc-bayern-muenchen"] == "2"


def test_parse_lineup_extracts_the_starting_eleven():
    lineup = parse_lineup(FIXTURE.read_text(encoding="utf-8"))
    starters = [p.name for p in lineup.players if p.status == "startelf"]
    alternatives = [p.name for p in lineup.players if p.status == "alternative"]
    assert len(starters) == 12  # 1 + 4 + 4 + 3
    assert starters[0] == "Neuer"
    assert "Laimer" in starters and "Luis Díaz" in starters
    # The display:none candidate of a position battle is kept as the alternative.
    assert alternatives == ["Stanišić"]


def test_parse_lineup_skips_alternatives_without_space_in_style():
    html = (
        "<div class='stadium_container_bg'>"
        "<div class='player_position_row'>"
        "<div class='player_position_column'>"
        "<div class='sub_child' style='display:block'>"
        "<div class='player_name'><a itemprop='athlete'>Laimer</a></div></div>"
        "<div class='sub_child' style='display:none'>"
        "<div class='player_name'><a itemprop='athlete'>Stanišić</a></div></div>"
        "</div>"
        + "".join(
            f"<div class='player_position_column'>"
            f"<div class='player_name'><a itemprop='athlete'>P{i}</a></div></div>"
            for i in range(2, 12)
        )
        + "</div></div>"
    )
    players = parse_lineup(html).players
    starters = [p.name for p in players if p.status == "startelf"]
    assert len(starters) == 11 and "Laimer" in starters and "Stanišić" not in starters
    assert [p.name for p in players if p.status == "alternative"] == ["Stanišić"]


def test_parse_lineup_raises_when_the_format_changed():
    with pytest.raises(FormatError):
        parse_lineup("<html><body>ganz anders</body></html>")
    sparse = (
        "<div class='stadium_container_bg'>"
        "<div class='player_position_row'>"
        "<div class='player_position_column'>"
        "<div class='player_name'><a itemprop='athlete'>Neuer</a></div>"
        "</div></div></div>"
    )
    with pytest.raises(FormatError):
        parse_lineup(sparse)


async def test_store_lineup_replaces_and_matches(sessions):
    known = [
        KnownPlayer("29-168", "Michel", "29"),
        KnownPlayer("29-1964", "Götze", "29"),
        KnownPlayer("29-2471", "Schubert", "29"),
    ]
    players = [LineupPlayer("Michel"), LineupPlayer("Schubert"), LineupPlayer("Unbekannt")]
    stored, unmatched = await store_lineup(sessions, "29", players, known, NOW)
    assert (stored, unmatched) == (3, 1)
    predicted = await predicted_players(sessions, NOW)
    assert predicted["29"] == TeamPrediction(frozenset({"29-168", "29-2471"}), ("Unbekannt",))
    # Götze isn't listed, but "Unbekannt" might be him: unknown, never "not in the eleven".
    assert prediction_for(predicted, "29", "29-1964") == Prognosis(None, unmatched=("Unbekannt",))
    assert prediction_for(predicted, "29", "29-168").starting is True
    assert prediction_for(predicted, "3", "3-1") == Prognosis()  # no prediction for the club

    stored, unmatched = await store_lineup(sessions, "29", [LineupPlayer("Götze")], known, NOW)
    assert (stored, unmatched) == (1, 0)
    predicted = await predicted_players(sessions, NOW, "29")
    assert predicted == {"29": TeamPrediction(frozenset({"29-1964"}))}
    # All names matched: a player who isn't listed is really not in the eleven.
    assert prediction_for(predicted, "29", "29-168") == Prognosis(False)


async def test_position_battle_alternative(sessions):
    known = [KnownPlayer("29-168", "Michel", "29"), KnownPlayer("29-1964", "Götze", "29")]
    players = [LineupPlayer("Michel"), LineupPlayer("Götze", "alternative")]
    await store_lineup(sessions, "29", players, known, NOW)
    predicted = await predicted_players(sessions, NOW)
    assert prediction_for(predicted, "29", "29-1964") == Prognosis(None, battle=True)
    assert prediction_for(predicted, "29", "29-168") == Prognosis(True)


async def test_store_lineup_never_wipes_with_an_empty_list(sessions):
    known = [KnownPlayer("29-168", "Michel", "29")]
    await store_lineup(sessions, "29", [LineupPlayer("Michel")], known, NOW)
    stored, _ = await store_lineup(sessions, "29", [], known, NOW)
    assert stored == 0
    assert (await predicted_players(sessions, NOW))["29"].starting == {"29-168"}


def test_client_raises_format_error_on_non_200():
    async def run():
        client = LigaInsiderClient(
            load_config(),
            transport=httpx.MockTransport(lambda r: httpx.Response(404)),
            pacer=DomainPacer(0, sleep=no_sleep),
        )
        try:
            with pytest.raises(FormatError):
                await client.fetch_lineup("2")
        finally:
            await client.aclose()

    import asyncio

    asyncio.run(run())


def test_client_paces_requests():
    import asyncio

    sleeps: list[float] = []

    async def fake_sleep(sec: float):
        sleeps.append(sec)

    async def run():
        client = LigaInsiderClient(
            load_config(),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))
            ),
            pacer=DomainPacer(2.0, monotonic=lambda: 0.0, sleep=fake_sleep),
        )
        try:
            await client.fetch_lineup("2")
            await client.fetch_lineup("2")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert sleeps == [2.0]  # first request unpaced, the second waits


def test_pacer_is_shared_per_domain_and_paces_redirects():
    import asyncio

    from app.lineups.ligainsider import shared_pacer

    assert shared_pacer("www.ligainsider.de") is shared_pacer("www.ligainsider.de")
    a, b = LigaInsiderClient(load_config()), LigaInsiderClient(load_config())
    assert a.pacer is b.pacer  # two jobs, one rule: at most one request every 2 s

    sleeps: list[float] = []

    async def fake_sleep(sec: float):
        sleeps.append(sec)

    def handler(request):
        if request.url.path == "/old/":
            return httpx.Response(301, headers={"Location": "/new/"})
        return httpx.Response(200, text="ok")

    async def run():
        client = LigaInsiderClient(
            load_config(),
            transport=httpx.MockTransport(handler),
            pacer=DomainPacer(2.0, monotonic=lambda: 0.0, sleep=fake_sleep),
        )
        try:
            await client._get("/old/")
        finally:
            await client.aclose()
        await a.aclose()
        await b.aclose()

    asyncio.run(run())
    assert sleeps == [2.0]  # the redirect hop waited too


class _StubClient:
    def __init__(self, teams: dict[str, str], names: list[str]):
        self.config = type("Cfg", (), {"teams": teams})()
        self.players = [LineupPlayer(n) for n in names]

    async def fetch_lineup(self, team_id: str):
        return type("Parsed", (), {"players": self.players})()

    async def aclose(self):
        pass


class _Ctx:
    def __init__(self, sessions):
        from tests.fakes import MemoryKV

        self.sessions = sessions
        self.kv = MemoryKV()

    def clock(self):
        return NOW


async def test_collect_lineup_predictions_runs_every_team(sessions):
    async with sessions.begin() as session:
        session.add(Team(id="29", name="Paderborn", image=None, updated_at=NOW))
        session.add(
            Player(
                id="29-168",
                name="Michel",
                team_id="29",
                position=4,
                status=0,
                market_value=1,
                updated_at=NOW,
            )
        )

    client = _StubClient({"29": "paderborn/1249", "2": "bayern/1"}, ["Michel", "Götze", "Schubert"])
    summary = await collect_lineup_predictions(_Ctx(sessions), client=client)
    assert summary.startswith("2 Vereine, 6 Einträge")
    assert "5 nicht zugeordnet" in summary  # only "Michel" of team 29 matches
    async with sessions() as session:
        total = len(list((await session.execute(select(LineupPrediction))).scalars()))
    assert total == 6  # 3 players × 2 teams


class _FailingClient:
    def __init__(self, teams: dict[str, str]):
        self.config = type("Cfg", (), {"teams": teams})()

    async def fetch_lineup(self, team_id: str):
        if team_id == "2":
            raise FormatError("Kaputt")
        return type("Parsed", (), {"players": [LineupPlayer("Michel")]})()

    async def aclose(self):
        pass


async def test_collect_lineup_predictions_continues_after_unreadable_team(sessions):
    async with sessions.begin() as session:
        session.add(Team(id="29", name="Paderborn", image=None, updated_at=NOW))
        session.add(
            Player(
                id="29-168",
                name="Michel",
                team_id="29",
                position=4,
                status=0,
                market_value=1,
                updated_at=NOW,
            )
        )
    client = _FailingClient({"2": "bayern/1", "29": "paderborn/1249"})
    ctx = _Ctx(sessions)
    summary = await collect_lineup_predictions(ctx, client=client)
    assert summary.startswith("1 Vereine, 1 Einträge")
    assert "1 nicht lesbar" in summary
    assert (await ctx.kv.get(LINEUPS_STATUS_KEY))["unreadable"] == ["2"]
    hint = await ligainsider_hint(sessions, ctx.kv, NOW)
    assert hint == "Startelf-Prognose zuletzt nicht lesbar für 2."


async def test_collect_lineup_predictions_fails_when_no_club_is_readable(sessions):
    async with sessions.begin() as session:
        session.add(
            Player(
                id="29-168",
                name="Michel",
                team_id="29",
                position=4,
                status=0,
                market_value=1,
                updated_at=NOW,
            )
        )
    client = _FailingClient({"2": "bayern/1"})
    with pytest.raises(FormatError, match="Keine Aufstellung lesbar"):
        await collect_lineup_predictions(_Ctx(sessions), client=client)


async def test_outdated_predictions_and_injuries_are_ignored(sessions):
    from datetime import timedelta

    from tests.fakes import MemoryKV

    known = [KnownPlayer("29-168", "Michel", "29"), KnownPlayer("29-1964", "Götze", "29")]
    await store_lineup(sessions, "29", [LineupPlayer("Michel")], known, NOW)
    teams = [TeamInjuries("paderborn", [Injury("Götze", "verletzung", None, None)])]
    await store_injuries(sessions, teams, {"paderborn": "29"}, known, NOW)
    later = NOW + timedelta(hours=40)
    assert await predicted_players(sessions, later) == {}
    assert await injury_for_player(sessions, "29-1964", later) is None
    hint = await ligainsider_hint(sessions, MemoryKV(), later)
    assert "Keine aktuelle Startelf-Prognose für 29" in hint
    assert "Verletztenliste ist älter als 36 h" in hint
    assert await ligainsider_hint(sessions, MemoryKV(), NOW) is None


def test_parse_injuries_extracts_and_skips_empty_clubs():
    parsed = parse_injuries(VERLETZTE.read_text(encoding="utf-8"))
    teams = {t.slug: t for t in parsed.teams}
    assert set(teams) == {"fc-bayern-muenchen", "borussia-dortmund"}  # Paderborn has no entries
    bayern = teams["fc-bayern-muenchen"].entries
    assert bayern[0].name == "Emre Can"
    assert bayern[0].kind == "verletzung"
    assert bayern[0].reason == "Kreuzbandriss"
    assert bayern[0].since == "7 Monaten und 1 Woche"
    dortmund = teams["borussia-dortmund"].entries
    assert dortmund[0].kind == "nicht_im_kader"
    assert dortmund[0].reason == "Nicht berücksichtigt"
    assert dortmund[0].since is None


def test_parse_injuries_raises_when_the_format_changed():
    with pytest.raises(FormatError):
        parse_injuries("<html><body>ganz anders</body></html>")


async def test_store_injuries_matches_and_replaces(sessions):
    known = [KnownPlayer("2-1812", "Emre Can", "2")]
    teams = [
        TeamInjuries(
            "fc-bayern-muenchen",
            [Injury("Emre Can", "verletzung", "Kreuzbandriss", "7 Monaten")],
        )
    ]
    stored, unmatched, skipped = await store_injuries(
        sessions, teams, {"fc-bayern-muenchen": "2"}, known, NOW
    )
    assert (stored, unmatched, skipped) == (1, 0, [])
    note = await injury_for_player(sessions, "2-1812", NOW)
    assert note == InjuryNote("verletzung", "Kreuzbandriss", "7 Monaten")

    # A new fetch without that player replaces the old one.
    stored, _, _ = await store_injuries(sessions, [], {}, known, NOW)
    assert stored == 0
    assert await injury_for_player(sessions, "2-1812", NOW) is None


def test_parse_injuries_normalizes_soft_hyphen_and_unknown_icon():
    html = (
        "<div class='personal_table'>"
        "<div class='leg_table_title'>"
        "<img src='https://x/images/teams/medium/fc-bayern-muenchen-wappen.png'/></div>"
        "<div class='small_table_holder'><div class='small_table_row'>"
        "<div class='small_table_column1'>"
        "<div class='left_icon'><img src='https://x/icon/komisch.png' alt='Muskelfaserriss'/></div>"
        "<a href='/x_1/'><div class='left_title'><strong>Nme&shy;cha</strong></div></a>"
        "</div>"
        "<div class='small_table_column2'><span></span></div>"
        "<div class='small_table_column4'><span></span></div>"
        "</div></div></div>"
    )
    entry = parse_injuries(html).teams[0].entries[0]
    assert entry.name == "Nmecha"  # soft hyphen removed
    assert entry.kind == "unbekannt"  # unknown icon, alt is not a known kind


async def test_store_injuries_skips_unknown_and_fails_when_none_map(sessions):
    known = [KnownPlayer("2-1812", "Emre Can", "2")]
    teams = [
        TeamInjuries("fc-bayern-muenchen", [Injury("Emre Can", "verletzung", None, None)]),
        TeamInjuries("unbekannt", [Injury("X", "verletzung", None, None)]),
    ]
    stored, unmatched, skipped = await store_injuries(
        sessions, teams, {"fc-bayern-muenchen": "2"}, known, NOW
    )
    assert (stored, unmatched, skipped) == (1, 0, ["unbekannt"])

    only_unknown = [TeamInjuries("unbekannt", [Injury("X", "verletzung", None, None)])]
    with pytest.raises(FormatError):
        await store_injuries(sessions, only_unknown, {"fc-bayern-muenchen": "2"}, known, NOW)


class _InjuryStub:
    def __init__(self, teams, team_by_slug):
        self.config = type("Cfg", (), {"team_by_slug": team_by_slug})()
        self._parsed = type("Parsed", (), {"teams": teams})()

    async def fetch_injuries(self):
        return self._parsed

    async def aclose(self):
        pass


async def test_collect_injuries_job(sessions):
    async with sessions.begin() as session:
        session.add(Team(id="2", name="Bayern", image=None, updated_at=NOW))
        session.add(
            Player(
                id="2-1812",
                name="Emre Can",
                team_id="2",
                position=3,
                status=1,
                market_value=1,
                updated_at=NOW,
            )
        )
    teams = [
        TeamInjuries(
            "fc-bayern-muenchen", [Injury("Emre Can", "verletzung", "Kreuzbandriss", "7 Monaten")]
        )
    ]
    client = _InjuryStub(teams, {"fc-bayern-muenchen": "2"})
    summary = await collect_injuries(_Ctx(sessions), client=client)
    assert summary.startswith("1 Ausfälle")
    assert await injury_for_player(sessions, "2-1812", NOW) is not None


async def test_unmatched_alternative_keeps_the_prognosis_clear(sessions):
    known = [KnownPlayer("29-168", "Michel", "29"), KnownPlayer("29-1964", "Götze", "29")]
    players = [LineupPlayer("Michel"), LineupPlayer("Unbekannt", "alternative")]
    await store_lineup(sessions, "29", players, known, NOW)
    predicted = await predicted_players(sessions, NOW)
    assert predicted["29"].unmatched == ()
    assert prediction_for(predicted, "29", "29-1964") == Prognosis(False)


async def test_hint_ignores_clubs_that_are_no_longer_scraped(sessions):
    from datetime import timedelta

    from tests.fakes import MemoryKV

    known = [KnownPlayer("x-1", "Michel", "relegated")]
    await store_lineup(sessions, "relegated", [LineupPlayer("Michel")], known, NOW)
    assert await ligainsider_hint(sessions, MemoryKV(), NOW + timedelta(hours=40)) is None


# Structural regression fixtures: original nesting with synthetic text and no page scripts.
REAL_LINEUP = Path(__file__).parent / "fixtures" / "ligainsider" / "markup_aufstellung.html"
REAL_INJURIES = Path(__file__).parent / "fixtures" / "ligainsider" / "markup_verletzte.html"


def test_real_lineup_page():
    players = parse_lineup(REAL_LINEUP.read_text(encoding="utf-8")).players
    starters = [p.name for p in players if p.status == "startelf"]
    alternatives = [p.name for p in players if p.status == "alternative"]
    assert len(starters) == 11
    assert starters[0] == "Fixture Spieler 1"
    assert len(set(starters)) == 11
    assert alternatives and set(alternatives).isdisjoint(starters)


def test_real_injury_page_maps_every_club_and_icon():
    parsed = parse_injuries(REAL_INJURIES.read_text(encoding="utf-8"))
    team_by_slug = load_config().team_by_slug
    assert len(parsed.teams) >= 15
    assert [t.slug for t in parsed.teams if t.slug not in team_by_slug] == []
    kinds = {e.kind for t in parsed.teams for e in t.entries}
    assert "unbekannt" not in kinds  # every icon of the real page is known
    assert "angeschlagen" in kinds  # "Schwerer angeschlagen"


async def test_hoffenheim_crest_with_an_injured_player_is_stored(sessions):
    # The saved real page has no Hoffenheim injuries, so the all-clubs test skips it.
    # Add a row using the site's real markup to exercise parsing and the worker mapping.
    soup = BeautifulSoup(REAL_INJURIES.read_text(encoding="utf-8"), "html.parser")
    crest = soup.select_one('img[src$="/medium/tsg-1899-hoffenheim-wappen.png"]')
    block = crest.find_parent("div", class_="personal_table")
    row = soup.select_one(".personal_table .small_table_row:has(.left_title strong)")
    row.select_one(".left_title strong").string = "Fixture Spieler"
    block.select_one(".small_table_row").replace_with(row)
    parsed = parse_injuries(str(soup))
    club = next(t for t in parsed.teams if t.slug == "tsg-1899-hoffenheim")
    async with sessions.begin() as session:
        session.add(Team(id="14", name="Hoffenheim", image=None, updated_at=NOW))
        session.add(
            Player(
                id="fixture-14",
                name="Fixture Spieler",
                team_id="14",
                position=3,
                status=1,
                market_value=1,
                updated_at=NOW,
            )
        )
    ctx = _Ctx(sessions)
    from app.lineups.service import INJURIES_STATUS_KEY

    await ctx.kv.set(INJURIES_STATUS_KEY, {"skipped": [club.slug]})
    summary = await collect_injuries(ctx, client=_InjuryStub([club], load_config().team_by_slug))
    assert summary == "1 Ausfälle"
    assert await injury_for_player(sessions, "fixture-14", NOW) is not None
    assert (await ctx.kv.get(INJURIES_STATUS_KEY))["skipped"] == []


def test_config_maps_all_clubs_once():
    config = load_config()
    assert len(config.teams) == 18
    assert len(set(config.teams.values())) == 18
