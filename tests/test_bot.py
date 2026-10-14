import copy

import pytest
from sqlalchemy import create_engine

from app.bot.conversation import answer
from app.bot.tools import ToolContext, _parse_market_query, run_tool
from app.db.models import Base, MatchdayResult, Player
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.league import ResolvedLeague
from app.services.watchlist import add as watchlist_add_row
from app.services.watchlist import set_threshold
from app.web.formatting import money_short
from tests.fakes import (
    NOW,
    FakeKickbase,
    FakeOpenLigaDb,
    add_recommendation_endpoints,
    fake_with_team_data,
    fixture,
    make_client,
    make_oldb_client,
)

LEAGUE = ResolvedLeague("1000002", "Testliga", "1")


class FakeLlm:
    """Plain-text answers in order (the bot uses complete_text)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    @property
    def calls(self):
        return len(self.prompts)

    async def complete_text(self, task, system, user):
        self.prompts.append(user)
        return type("C", (), {"data": self.replies.pop(0)})()


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'bot.db'}"
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


async def ask(fake, sessions, steps, question, history=None):
    llm = FakeLlm(steps)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        ctx = ToolContext(client, sessions, LEAGUE, oldb, NOW)
        reply = await answer(llm, ctx, question, history)
    return reply, llm


async def test_situation_answers_in_one_call(fake, sessions):
    reply, llm = await ask(fake, sessions, ["Du hast 17,01 Mio. auf dem Konto."], "Budget?")
    assert reply == "Du hast 17,01 Mio. auf dem Konto."
    assert llm.calls == 1  # no tool needed: the numbers are in the situation
    prompt = llm.prompts[0]
    for part in (
        "LAGEBILD:",
        "Kontostand: 17,01",
        "Nächste Aufstellungs-Deadline für den 5. Spieltag",
        "AKTUELLE AUFSTELLUNG (9 von 11 Plätzen)",
        "2 Plätze leer",
        "KADER (",
        "Hansen (ABW",
        "VERKAUFSKANDIDAT",
        "KAUFEMPFEHLUNGEN",
        "Deine offenen Gebote:",
        "Liga: Platz 2 von 6",
    ):
        assert part in prompt, part
    # C4.3: jede Kaufempfehlung nennt neben Score auch xP – Lagebild und Tool nutzen denselben
    # buy_line, daher reicht die Stichprobe im Lagebild.
    assert "Aufstellungsindex" in prompt
    # xP wird als 0..100 %-Wert gemeldet (nicht als 0..1-Rohwert wie „0,3 xP").
    import re

    assert re.search(r"Aufstellungsindex \d{1,3}/100", prompt), prompt
    assert "keine Punkteprognose" in prompt
    assert "% xP" not in prompt


async def test_situation_never_names_other_managers(fake, sessions):
    _, llm = await ask(fake, sessions, ["Ok."], "Markt?")
    sellers = {p["u"]["n"] for p in fixture("league_market")["it"] if p.get("u")}
    assert sellers and not any(name in llm.prompts[0] for name in sellers)


async def test_situation_omits_watchlist_when_empty(fake, sessions):
    """Without entries the MERKLISTE block must not appear – the AI doesn't need to know."""
    _, llm = await ask(fake, sessions, ["Ok."], "Budget?")
    assert "MERKLISTE" not in llm.prompts[0]


async def _seed_player(
    session_factory, pid: str, name: str, status: int = 1, value: int = 1_000_000
):
    async with session_factory.begin() as session:
        session.add(
            Player(
                id=pid,
                name=name,
                team_id="t",
                position=1,
                status=status,
                market_value=value,
                updated_at=NOW,
            )
        )


async def test_situation_includes_watchlist(fake, sessions):
    """Watched players: status, on-market yes/no, threshold – no seller names."""
    await _seed_player(sessions, "577", "Batz")  # listed in league_market fixture
    await _seed_player(sessions, "9999", "Zebra")  # not on the market
    await watchlist_add_row(sessions, LEAGUE.id, "577", NOW)
    await watchlist_add_row(sessions, LEAGUE.id, "9999", NOW)
    await set_threshold(sessions, LEAGUE.id, "577", 750_000)

    _, llm = await ask(fake, sessions, ["Ok."], "Was gibt es Neues?")
    prompt = llm.prompts[0]
    assert "MERKLISTE" in prompt
    # Listed player: market price + threshold
    assert "Batz:" in prompt
    assert "auf Markt ja" in prompt
    assert f"Preis {money_short(500_000)}" in prompt  # league_market fixture: Batz 500000
    assert f"Schwelle {money_short(750_000)}" in prompt
    # Off-market player: just status / not listed
    assert "Zebra:" in prompt
    assert "auf Markt nein" in prompt
    # No seller names leaked
    sellers = {p["u"]["n"] for p in fixture("league_market")["it"] if p.get("u")}
    assert not any(name in prompt for name in sellers)


# ---------------------------------------------------------------- own-offers section (C2.4)


def _own_listing_market(mv: int = 500_000, bid: int = 700_000, offer_count: int = 1) -> dict:
    """A market with a single entry sold by `9000001` (the manager from `user_me.json`)
    carrying one or more offers above the market value. The Caci/Røssing-Lelesiit entries
    from the live fixture are Kickbase sales, so they are stripped here to keep the test
    minimal."""
    market = copy.deepcopy(fixture("league_market"))
    market["it"] = [
        {
            "i": "577",
            "fn": "Daniel",
            "n": "Batz",
            "tid": "15",
            "pos": 1,
            "st": 0,
            "mvt": 0,
            "mv": mv,
            "ofc": offer_count,
            "exs": 3600,
            "prc": mv,
            "isn": False,
            "iposl": False,
            "dt": "2026-10-09T12:00:00Z",
            "u": {"i": "9000001", "n": "Manager 1", "st": 0},
            "ofs": [{"uop": bid, "uoid": "9000002", "st": 0, "unm": "Manager Geheim"}],
        }
    ]
    return market


def _fake_with_own_listing(*, bid: int = 700_000, offer_count: int = 1) -> FakeKickbase:
    """A fresh FakeKickbase with the team endpoints and a single market entry sold by the
    manager (see `_own_listing_market`). Other listings from the live fixture are dropped so
    this test focuses on the own-offers section."""
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    fake.on(
        "GET",
        "/v4/leagues/1000002/market",
        _own_listing_market(bid=bid, offer_count=offer_count),
    )
    return fake


async def test_situation_omits_own_offers_when_nothing_of_mine_is_bid_on(fake, sessions):
    """Without an own listing with bids the GEBOTE-AUF-MEINE-SPIELER block must not appear."""
    _, llm = await ask(fake, sessions, ["Ok."], "Budget?")
    assert "GEBOTE AUF MEINE SPIELER" not in llm.prompts[0]


async def test_situation_includes_own_offers(fake, sessions):
    """An own listing with a bid: name, best bid, market value, number of bids."""
    fresh = _fake_with_own_listing(bid=700_000, offer_count=2)

    _, llm = await ask(fresh, sessions, ["Ok."], "Was gibt es?")
    prompt = llm.prompts[0]
    assert "GEBOTE AUF MEINE SPIELER" in prompt
    assert "Daniel Batz" in prompt
    assert f"bestes Gebot {money_short(700_000)}" in prompt
    assert f"Marktwert {money_short(500_000)}" in prompt
    assert "2 Gebote" in prompt  # plural
    assert "annehmen nur in der Kickbase-App" in prompt
    # No bidder names leaked: the fixture has "Manager Geheim" as bidder name, manager
    # "Manager 1" as seller and managers 3/5/6 as other sellers in the stripped market.
    for forbidden in ("Manager Geheim", "9000002"):
        assert forbidden not in prompt


async def test_situation_own_offers_singular_when_one_bid(fake, sessions):
    """One bid → singular "1 Gebot" (no trailing "e"), not "1 Gebote"."""
    fresh = _fake_with_own_listing(bid=700_000, offer_count=1)

    _, llm = await ask(fresh, sessions, ["Ok."], "Was gibt es?")
    prompt = llm.prompts[0]
    assert "GEBOTE AUF MEINE SPIELER" in prompt
    assert "1 Gebot)" in prompt  # singular, no "e"
    assert "1 Gebote)" not in prompt  # not the plural form


# ---------------------------------------------------------------- letzer Spieltag (C3.5)  # noqa: E501


async def _seed_matchday_results(session_factory, *rows: MatchdayResult) -> None:
    async with session_factory.begin() as session:
        for row in rows:
            session.add(row)


async def test_situation_omits_last_matchday_when_no_row(fake, sessions):
    """Without a stored matchday the LETZTER SPIELTAG block must not appear – the AI
    doesn't need to know the concept exists."""
    _, llm = await ask(fake, sessions, ["Ok."], "Wie lief der letzte Spieltag?")
    assert "LETZTER SPIELTAG" not in llm.prompts[0]


async def test_situation_includes_last_matchday_summary(fake, sessions):
    """One stored matchday: points, placement, "1. Spieltag" instead of an arrow, and
    missed_points when present. A second matchday is added so the placement delta (▲)
    is exercised as well."""
    await _seed_matchday_results(
        sessions,
        MatchdayResult(
            league_id=LEAGUE.id,
            season=2026,
            matchday=1,
            points=42,
            placement=5,
            matchday_placement=4,
            team_value=180_000_000,
            missed_points=None,  # row written before the C3.5 column existed
            created_at=NOW,
        ),
        MatchdayResult(
            league_id=LEAGUE.id,
            season=2026,
            matchday=2,
            points=71,
            placement=3,
            matchday_placement=2,
            team_value=181_000_000,
            missed_points=12,  # row written after the column existed
            created_at=NOW,
        ),
    )

    _, llm = await ask(fake, sessions, ["Ok."], "Wie lief der letzte Spieltag?")
    prompt = llm.prompts[0]
    block = _extract_last_matchday_block(prompt)
    assert block is not None, "LETZTER SPIELTAG block missing"
    # Latest is matchday 2 (71 points, Platz 3, up by 2).
    assert "(2.):" in block
    assert "71 Punkte" in block
    assert "Platz 3" in block
    assert "▲2" in block
    assert "Spieltagsplatz 2" in block
    assert "verpasste Punkte 12" in block
    # Matchday 1 (the older row) must NOT appear here – the Lagebild only shows the latest.
    assert "42 Punkte" not in block
    # Old row without missed_points must not leak a "verpasste Punkte" line either.
    assert "verpasste Punkte None" not in block


async def test_situation_last_matchday_first_matchday_marker(fake, sessions):
    """First matchday (no previous placement) → '1. Spieltag' marker, no ▲/▼."""
    await _seed_matchday_results(
        sessions,
        MatchdayResult(
            league_id=LEAGUE.id,
            season=2026,
            matchday=1,
            points=42,
            placement=5,
            matchday_placement=None,
            team_value=180_000_000,
            missed_points=0,
            created_at=NOW,
        ),
    )
    _, llm = await ask(fake, sessions, ["Ok."], "Wie lief?")
    block = _extract_last_matchday_block(llm.prompts[0])
    assert block is not None
    assert "(1.):" in block
    assert "1. Spieltag" in block
    assert "▲" not in block and "▼" not in block
    assert "verpasste Punkte 0" in block


def _extract_last_matchday_block(prompt: str) -> str | None:
    """Return the single line that starts with "LETZTER SPIELTAG" (or None)."""
    for line in prompt.splitlines():
        if line.startswith("LETZTER SPIELTAG"):
            return line
    return None


async def test_minus_is_flagged_in_the_situation(fake, sessions):
    budget = fixture("league_budget")
    budget["b"], budget["pbaa"] = -9_180_000.0, -9_180_000.0
    fake.on("GET", "/v4/leagues/1000002/me/budget", budget)
    _, llm = await ask(fake, sessions, ["Ok."], "Was tun?")
    assert "IM MINUS" in llm.prompts[0] and "AUS DEM MINUS: es fehlen" in llm.prompts[0]


async def test_tool_line_runs_the_tool_then_answers(fake, sessions):
    replies = ["WERKZEUG: markt(TW)", "Schau dir die Torhüter an."]
    reply, llm = await ask(fake, sessions, replies, "Was gibt es Neues?")
    assert reply == "Schau dir die Torhüter an."
    assert "DATEN markt(TW):" in llm.prompts[1]


async def test_market_question_is_prefetched(fake, sessions):
    _, llm = await ask(fake, sessions, ["Ok."], "Welcher Torwart lohnt sich?")
    assert llm.calls == 1 and "DATEN markt(Welcher Torwart lohnt sich?)" in llm.prompts[0]


async def test_tool_line_after_an_intro_runs_the_tool(fake, sessions):
    replies = ["Ich schaue nach.\nWERKZEUG: markt(TW)", "Torhüter: siehe oben."]
    reply, llm = await ask(fake, sessions, replies, "Hallo?")
    assert reply == "Torhüter: siehe oben."
    assert "DATEN markt(TW):" in llm.prompts[1]


async def test_tool_line_without_rounds_left_is_dropped_from_the_answer(fake, sessions):
    replies = ["WERKZEUG: markt(TW)", "WERKZEUG: markt(ST)", "Alles gut.\nWERKZEUG: markt(MF)"]
    reply, llm = await ask(fake, sessions, replies, "Hallo?")
    assert reply == "Alles gut." and llm.calls == 3


async def test_a_stubborn_model_is_capped(fake, sessions):
    reply, llm = await ask(fake, sessions, ["WERKZEUG: loeschen(x)"] * 50, "Hallo?")
    assert "erlaubten Zahl von Schritten" in reply
    assert llm.calls == 2  # refused once with a note, refused again: stop


async def test_calls_per_question_never_exceed_the_cap(fake, sessions):
    from app.bot.conversation import MAX_CALLS

    replies = ["WERKZEUG: markt(TW)", "WERKZEUG: markt(ST)", "Kostet 140 Mio.", "Kostet 141 Mio."]
    reply, llm = await ask(fake, sessions, replies + ["nie"] * 10, "Was kostet X?")
    assert llm.calls == MAX_CALLS == 4
    assert "Ohne Gewähr" in reply


async def test_numbers_from_earlier_answers_are_not_trusted(fake, sessions):
    history = [("Was kostet X?", "X kostet 123,45 Mio.")]
    replies = ["Ja, 123,45 Mio.", "Ja, 123,45 Mio."]
    reply, _ = await ask(fake, sessions, replies, "Wirklich?", history)
    assert "Ohne Gewähr" in reply


async def test_old_tool_names_still_work(fake, sessions):
    _, llm = await ask(fake, sessions, ["WERKZEUG: marktangebote()", "Ok."], "Hallo")
    assert "DATEN markt():" in llm.prompts[1]


async def test_history_is_passed_for_follow_ups(fake, sessions):
    history = [("Soll ich Hansen verkaufen?", "Ja, sein Marktwert fällt.")]
    _, llm = await ask(fake, sessions, ["Ok."], "Und Maza?", history)
    assert "BISHERIGER CHAT" in llm.prompts[0]
    assert "Nutzer: Soll ich Hansen verkaufen?" in llm.prompts[0]


async def test_invented_numbers_get_one_repair_round(fake, sessions):
    replies = ["Wirtz kostet 140 Mio.", "Dazu habe ich keine Daten."]
    reply, llm = await ask(fake, sessions, replies, "Was kostet Wirtz?")
    assert reply == "Dazu habe ich keine Daten."
    assert "nicht in den Daten stehen: 140" in llm.prompts[1]


async def test_invented_numbers_twice_are_sent_with_a_warning(fake, sessions):
    replies = ["Wirtz kostet 140 Mio.", "Wirtz kostet 141 Mio."]
    reply, _ = await ask(fake, sessions, replies, "Was kostet Wirtz?")
    assert reply.startswith("Wirtz kostet 141 Mio.")
    assert "Ohne Gewähr" in reply and "141" in reply.splitlines()[-1]


async def test_unknown_tool_gets_an_answer_without_tools(fake, sessions):
    replies = ["WERKZEUG: loeschen(alles)", "Das kann ich nicht."]
    reply, llm = await ask(fake, sessions, replies, "Lösche alles!")
    assert reply == "Das kann ich nicht."
    assert "ohne weiteres Werkzeug" in llm.prompts[1]


async def test_tool_round_limit(fake, sessions):
    replies = ["WERKZEUG: markt(TW)"] * 3 + ["Fertig."]
    reply, llm = await ask(fake, sessions, replies, "Hallo")
    assert reply == "Fertig."
    assert llm.calls == 4  # two tool rounds, the third request is refused


async def test_without_situation_the_bot_still_answers():
    # No database: the situation fails, the question is still answered (tools may work).
    llm = FakeLlm(["Dazu fehlen mir gerade die Daten."])
    async with make_client(FakeKickbase()) as client:
        ctx = ToolContext(client, None, LEAGUE, None, NOW)
        reply = await answer(llm, ctx, "Budget?")
    assert reply == "Dazu fehlen mir gerade die Daten."
    assert "Lagebild gerade nicht verfügbar" in llm.prompts[0]


async def test_named_players_are_found_by_whole_name_part(sessions):
    from app.bot.tools import named_players
    from tests.test_players import seed

    await seed(sessions)
    assert await named_players(sessions, "Wie ist Hansen drauf?") == ["Hansen"]
    assert await named_players(sessions, "Wie ist Hans drauf?") == []  # no partial match
    assert await named_players(sessions, "Soll ich verkaufen?") == []


async def test_player_tool_adds_the_verdict(fake, sessions):
    from tests.test_player_pages import add_player_endpoints

    add_player_endpoints(fake)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        ctx = ToolContext(client, sessions, LEAGUE, oldb, NOW)
        result = await run_tool(ctx, "spieler", "6292")
    assert "IM EIGENEN KADER; Einschätzung:" in result.splitlines()[1]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ST bis 5 Mio", (4, 5_000_000, False)),
        ("Abwehr", (2, None, False)),
        ("Stürmer unter 2,5", (4, 2_500_000, False)),
        ("Welcher Stürmer lohnt sich am 7. Spieltag?", (4, None, False)),
        ("Abwehr für 3 Mio.", (2, 3_000_000, False)),
        ("MF 4", (3, 4_000_000, False)),
        ("alle", (0, None, True)),
        ("", (0, None, False)),
    ],
)
def test_parse_market_query(query, expected):
    assert _parse_market_query(query) == expected


async def test_market_tool_filters(fake, sessions):
    from app.bot.context import build_situation

    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        ctx = ToolContext(client, sessions, LEAGUE, oldb, NOW)
        ctx.situation = await build_situation(ctx, __import__("zoneinfo").ZoneInfo("UTC"))
        result = await run_tool(ctx, "markt", "TW")
    assert "passende Angebote" in result
    assert "(TW" in result and "(ST" not in result


async def test_tool_budget_and_kader():
    fake = FakeKickbase()
    fake.on("GET", "/v4/leagues/1000002/me/budget", fixture("league_budget"))
    fake.on("GET", "/v4/leagues/1000002/squad", fixture("league_squad"))
    async with make_client(fake) as client:
        ctx = ToolContext(client, None, LEAGUE, None, NOW)
        budget = await run_tool(ctx, "budget", None)
        assert budget == "Kontostand: 17,01 Mio.; nach allen offenen Geboten: 8,01 Mio."
        assert "Hansen" in await run_tool(ctx, "mein_kader", None)


async def test_tool_unknown_name():
    async with make_client(FakeKickbase()) as client:
        ctx = ToolContext(client, None, LEAGUE, None, NOW)
        assert "Unbekanntes Werkzeug" in await run_tool(ctx, "gibtsnicht", None)


async def test_spieler_by_id(sessions):
    from tests.test_players import player_fake

    async with make_client(player_fake()) as client:
        ctx = ToolContext(client, sessions, LEAGUE, None, NOW)
        result = await run_tool(ctx, "spieler", "6292")
    assert "Mattes" in result and "fehlgeschlagen" not in result


async def test_spieler_by_name_uses_the_search(sessions):
    from tests.test_players import player_fake, seed

    await seed(sessions)
    async with make_client(player_fake()) as client:
        ctx = ToolContext(client, sessions, LEAGUE, None, NOW)
        result = await run_tool(ctx, "spieler", "Hansen")
    assert "Mattes" in result  # the first search hit's details


@pytest.mark.parametrize("argument", ["../../../leagues/1/ranking", "12?x=1", "", "x"])
async def test_spieler_rejects_ids_and_names_that_are_neither(argument):
    fake = FakeKickbase()
    async with make_client(fake) as client:
        ctx = ToolContext(client, None, LEAGUE, None, NOW)
        for name in ("spieler", "spieler_details"):
            result = await run_tool(ctx, name, argument)
            assert result.startswith("Ungültige Spieler-ID")
    assert fake.gets() == []  # nothing reached Kickbase


async def test_tool_budget_mentions_minus():
    fake = FakeKickbase()
    budget = fixture("league_budget")
    budget["pbaa"] = budget["b"] = -5_000_000.0
    fake.on("GET", "/v4/leagues/1000002/me/budget", budget)
    async with make_client(fake) as client:
        ctx = ToolContext(client, None, LEAGUE, None, NOW)
        assert "im Minus" in await run_tool(ctx, "budget", None)
