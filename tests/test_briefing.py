import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine

from app.db.models import AiUsage, Base, NewsItem, NewsReport
from app.db.session import make_engine, make_sessionmaker
from app.kickbase.league import LeagueContext, ResolvedLeague
from app.news.service import EXTRACT_STATUS_KEY, FEED_STATUS_KEY
from app.notify.telegram import TelegramNotifier
from app.services.briefing import (
    Briefing,
    BriefingData,
    collect_briefing_data,
    compose_briefing,
    unknown_numbers,
    write_briefing,
)
from app.worker.news_jobs import AI_PAUSE_KEY, BRIEFING_KEY, daily_briefing
from tests.fakes import (
    TEAM_LEAGUE_ID,
    FakeClock,
    FakeLlm,
    FakeOpenLigaDb,
    FakeTelegram,
    MemoryKV,
    add_recommendation_endpoints,
    fake_with_team_data,
    make_client,
    make_llm,
    make_oldb_client,
)

NOW = datetime(2026, 10, 6, 5, 40, tzinfo=UTC)
TZ = ZoneInfo("Europe/Berlin")
LEAGUE = ResolvedLeague(TEAM_LEAGUE_ID, "Meine Liga", "1")
SQUAD_PLAYER = "8227"  # Vandevoordt in league_squad.json


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'briefing.db'}"
    sync = create_engine(url)
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = make_engine(url)
    yield make_sessionmaker(engine)
    await engine.dispose()


DATA = BriefingData(
    {
        "budget": "3,72 Mio.",
        "kaufempfehlungen": [{"name": "Kane", "preis": "5,6 Mio.", "bestandteile": "Form +20"}],
        "kader": [{"name": "Musiala", "trend_7_tage": "−8,3 %"}],
    },
    ("Budget: 3,72 Mio.", "Kaufen: Kane (5,6 Mio., Score 71)"),
    ("Programm zählt neutral.",),
)


@pytest.mark.parametrize(
    ("text", "unknown"),
    [
        ("Kane für 5,6 Mio. kaufen, Budget 3,72 Mio.", []),
        ("2 Spieler fehlen, Top 3, 11 Punkte", []),  # small whole numbers
        ("12 Punkte", ["12"]),
        ("Kane für 5,9 Mio. kaufen", ["5,9"]),
        ("Form 20", []),  # unsigned stands for "+20"
        ("Form -20", ["-20"]),  # but a sign must match
        ("Musiala −8,3 %", []),
        ("Musiala +8,3 %", ["+8,3"]),
    ],
)
def test_unknown_numbers(text, unknown):
    assert unknown_numbers(text, DATA.payload_json) == unknown


async def test_write_briefing_uses_checked_ai_text(sessions):
    fake = FakeLlm({"zeilen": ["Kane für 5,6 Mio. kaufen.", " ", "Budget: 3,72 Mio."]})
    llm = make_llm(sessions, fake, FakeClock(NOW))
    briefing = await write_briefing(llm, DATA)
    assert briefing.lines == ("Kane für 5,6 Mio. kaufen.", "Budget: 3,72 Mio.")
    assert briefing.by_ai
    prompt = fake.requests[0]["messages"][1]["content"]
    assert json.loads(prompt) == DATA.payload  # the model sees exactly our data
    await llm.aclose()


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        ({"zeilen": ["Kane für 7,1 Mio. kaufen."]}, "unbekannte Zahlen: ['7,1']"),
        ({"zeilen": []}, "keinen Text"),
    ],
)
async def test_write_briefing_falls_back(sessions, answer, note):
    llm = make_llm(sessions, FakeLlm(answer), FakeClock(NOW))
    briefing = await write_briefing(llm, DATA)
    assert briefing.lines == DATA.fallback_lines
    assert not briefing.by_ai
    assert note in briefing.note
    await llm.aclose()


def test_compose_briefing_escapes_and_adds_notes():
    text = compose_briefing(
        "A&B",
        Briefing(("Kaufe <Kane>",), True, ""),
        ["Programm <neutral>"],
        "https://kbh.test/empfehlungen",
    )
    lines = text.split("\n")
    assert lines[0] == "<b>Briefing A&amp;B</b>"
    assert lines[1] == "Kaufe &lt;Kane&gt;"
    assert lines[2] == "⚠️ Programm &lt;neutral&gt;"
    assert "KI-Text aus den Dashboard-Daten" in lines[3]
    assert '<a href="https://kbh.test/empfehlungen">Empfehlungen öffnen</a>' in lines[3]


async def add_squad_news(sessions, published=NOW, *, updated=None, revision=1):
    async with sessions.begin() as session:
        session.add(
            NewsItem(
                guid="g1",
                source="kicker",
                title="Torwart fehlt",
                teaser="",
                link="https://kicker.test/1",
                categories=[],
                published_at=published,
                fetched_at=NOW,
                processed_at=NOW,
                updated_at=updated,
                revision=revision,
            )
        )
        session.add(
            NewsReport(
                news_guid="g1",
                published_at=published,
                player_name="Maarten Vandevoordt",
                team_id="43",
                player_id=SQUAD_PLAYER,
                kind="verletzung",
                status="faellt_aus",
                absence="rund drei Wochen",
                expected_return=None,
                revision=revision,
            )
        )


async def test_collect_briefing_data(sessions):
    await add_squad_news(sessions)
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        data = await collect_briefing_data(client, oldb, sessions, LEAGUE, "Europe/Berlin", NOW)
    payload = data.payload
    assert payload["datum"] == "6.10."
    assert set(payload) == {
        "datum",
        "kontostand",
        "nach_offenen_geboten",  # the fixture has open bids (17,01 → 8,01 Mio.)
        "kader",
        "kaufempfehlungen",
        "verkaufskandidaten",
    }
    keeper = next(p for p in payload["kader"] if p["name"] == "Vandevoordt")
    assert keeper["meldung"].startswith("fällt aus (Verletzung: rund drei Wochen; kicker ")
    assert len(payload["kaufempfehlungen"]) <= 3
    for buy in payload["kaufempfehlungen"]:
        assert set(buy) == {
            "name",
            "verein",
            "position",
            "preis",
            "score",
            "bestandteile",
            "angebot_endet",
        }
    # Other managers never go to the model: not the sellers, not the league name
    # (it contains a person's name).
    assert "Manager" not in data.payload_json
    assert "Meine Liga" not in data.payload_json
    assert data.fallback_lines[0] == (
        "Kontostand: 17,01\u00a0Mio. (nach allen offenen Geboten 8,01\u00a0Mio.)"
    )
    assert any(line.startswith("Meldungen im Kader: Vandevoordt") for line in data.fallback_lines)


async def test_briefing_keeps_original_date_and_identifies_recent_revision(sessions):
    await add_squad_news(
        sessions, NOW - timedelta(days=20), updated=NOW - timedelta(hours=1), revision=2
    )
    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        data = await collect_briefing_data(client, oldb, sessions, LEAGUE, "Europe/Berlin", NOW)
    keeper = next(p for p in data.payload["kader"] if p["name"] == "Vandevoordt")
    assert "16.9." in keeper["meldung"]
    assert "Änderung erkannt 06:40 · Revision 2" in keeper["meldung"]


@pytest.fixture
async def briefing_ctx(sessions):
    from app.worker.jobs import JobContext

    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    clock = FakeClock(NOW)
    client = make_client(fake, clock=clock)
    oldb = make_oldb_client(FakeOpenLigaDb())
    telegram = FakeTelegram()
    kv = MemoryKV()
    notifier = TelegramNotifier("t", "42", transport=httpx.MockTransport(telegram.handler))
    ctx = JobContext(
        client=client,
        sessions=sessions,
        league=LeagueContext(client, kv, "Testliga"),
        kv=kv,
        tz=TZ,
        clock=clock,
        notifier=notifier,
        public_url="https://kbh.test",
        oldb=oldb,
    )
    ctx.telegram = telegram
    fresh = NOW.isoformat()
    await kv.set(FEED_STATUS_KEY, {"ok": True, "at": fresh})
    await kv.set(EXTRACT_STATUS_KEY, {"at": fresh, "state": "ok", "pending": 0, "skipped": 0})
    yield ctx
    await client.aclose()
    await oldb.aclose()
    await notifier.aclose()


async def test_daily_briefing_once_a_day(briefing_ctx):
    ctx = briefing_ctx
    llm = ctx.llm = make_llm(
        ctx.sessions, FakeLlm({"zeilen": ["Heute nichts Dringendes."]}), ctx.clock
    )
    assert await daily_briefing(ctx) == "Briefing gesendet (KI-Text von small)"
    assert await daily_briefing(ctx) == "Briefing heute schon gesendet"
    [text] = ctx.telegram.texts
    assert text.startswith("<b>Briefing Testliga</b>\nHeute nichts Dringendes.\n")
    assert await ctx.kv.get(BRIEFING_KEY) == "2026-10-06"
    await llm.aclose()


async def test_daily_briefing_mentions_incomplete_news(briefing_ctx):
    ctx = briefing_ctx
    await ctx.kv.set(
        EXTRACT_STATUS_KEY, {"at": NOW.isoformat(), "state": "failed", "detail": "HTTP 503"}
    )
    llm = ctx.llm = make_llm(ctx.sessions, FakeLlm({"zeilen": ["Alles ruhig."]}), ctx.clock)
    await daily_briefing(ctx)
    [text] = ctx.telegram.texts
    assert "⚠️ Meldungen: Auswertung der Meldungen zuletzt fehlgeschlagen (HTTP 503)." in text
    await llm.aclose()


async def test_daily_briefing_falls_back_without_answer(briefing_ctx):
    ctx = briefing_ctx
    fake = FakeLlm(httpx.Response(503, json={}), httpx.Response(503, json={}))
    llm = ctx.llm = make_llm(ctx.sessions, fake, ctx.clock)
    result = await daily_briefing(ctx)
    assert result.startswith("Briefing gesendet (KI-Fehler: KI-Anbieter antwortet mit HTTP 503")
    [text] = ctx.telegram.texts
    assert "\nKontostand: " in text
    assert "<i>ohne KI</i>" in text
    await llm.aclose()


async def test_daily_briefing_at_the_limit_sends_plain_and_pause_notice(briefing_ctx):
    ctx = briefing_ctx
    fake = FakeLlm()
    llm = ctx.llm = make_llm(ctx.sessions, fake, ctx.clock, calls_per_day=1)
    async with ctx.sessions.begin() as session:
        session.add(AiUsage(at=NOW, task="news", model="small", status="ok"))
    result = await daily_briefing(ctx)
    assert result.startswith("Briefing gesendet (KI pausiert: Tageslimit")
    briefing, pause = ctx.telegram.texts
    assert "<i>ohne KI</i>" in briefing
    assert pause.startswith("<b>KI pausiert bis morgen</b>")
    assert await ctx.kv.get(BRIEFING_KEY) == "2026-10-06"
    assert await ctx.kv.get(AI_PAUSE_KEY) == "day:2026-10-06"
    assert fake.requests == []
    await llm.aclose()


async def test_daily_briefing_needs_ai_and_telegram(briefing_ctx):
    ctx = briefing_ctx
    assert await daily_briefing(ctx) == "Briefing aus (KI nicht konfiguriert)"
    ctx.notifier = None
    assert await daily_briefing(ctx) == "Briefing aus (Telegram nicht konfiguriert)"


async def test_collect_briefing_data_mentions_the_sell_guide_when_in_minus(sessions):
    from tests.fakes import fixture

    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    budget = fixture("league_budget")
    budget["pbaa"] = -5_000_000.0
    budget["b"] = -5_000_000.0
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        data = await collect_briefing_data(client, oldb, sessions, LEAGUE, "Europe/Berlin", NOW)
    assert "im_minus" in data.payload
    assert data.payload["im_minus"]["fehlt"]
    assert any("Im Minus" in line for line in data.fallback_lines)


async def test_briefing_mentions_minus_only_if_bids_win(sessions):
    from tests.fakes import fixture

    fake = fake_with_team_data()
    add_recommendation_endpoints(fake)
    budget = fixture("league_budget")
    budget["b"], budget["pbaa"] = 2_000_000.0, -3_000_000.0
    fake.on("GET", f"/v4/leagues/{TEAM_LEAGUE_ID}/me/budget", budget)
    async with make_client(fake) as client, make_oldb_client(FakeOpenLigaDb()) as oldb:
        data = await collect_briefing_data(client, oldb, sessions, LEAGUE, "Europe/Berlin", NOW)
    assert "im_minus" not in data.payload
    assert data.payload["minus_falls_alle_gebote_gewinnen"] == "3,00\u00a0Mio."
    assert any(line.startswith("Gewinnen alle offenen Gebote") for line in data.fallback_lines)


@pytest.mark.parametrize(
    ("text", "source", "unknown"),
    [
        ("Du hast 17 Mio.", "Kontostand: 17,00 Mio.", []),  # by value, not by spelling
        ("um 8,3 % gestiegen", "Marktwert 7 Tage: +8.3 %", []),  # English decimal point
        ("für 25,0 Mio.", "Frage: Reicht es für 25 Mio?", []),
        ("fällt 3-4 Wochen aus", "Ausfall: 3-4 Wochen", []),  # a range, no minus
        ("Marktwert 1.234.567 €", "1.234.567", []),
        ("3,7 Mio.", "3,72 Mio.", ["3,7"]),  # rounding is still not allowed
        ("Form -20", "Form +20", ["-20"]),
    ],
)
def test_unknown_numbers_by_value(text, source, unknown):
    assert unknown_numbers(text, source) == unknown
