import dataclasses
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models import AiUsage, Base, NewsItem, NewsReport, Player, Team
from app.db.session import make_engine, make_sessionmaker
from app.db.stores import KeyValueStore
from app.kickbase.league import LeagueContext
from app.news.extract import OTHER_CLUB, ExtractedReport, build_prompt, extract_batch
from app.news.kicker import USER_AGENT, FeedError, KickerFeed, parse_feed
from app.news.matching import KnownPlayer, match_player, normalize
from app.news.service import (
    EXTRACT_STATUS_KEY,
    FEED_STATUS_KEY,
    MAX_ATTEMPTS,
    news_problem,
    reports_for_players,
    save_reports,
    store_items,
)
from app.notify.telegram import TelegramError, TelegramNotifier
from app.worker.news_jobs import AI_PAUSE_KEY, collect_news
from tests.fakes import (
    FakeClock,
    FakeLlm,
    FakeTelegram,
    MemoryKV,
    fake_with_team_data,
    llm_response,
    make_client,
    make_llm,
)

FEED = (Path(__file__).parent / "fixtures" / "kicker" / "bundesliga.xml").read_bytes()
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TZ = ZoneInfo("Europe/Berlin")
VANDEVOORDT = "8227"  # in the test squad (league_squad.json), club 43 = Leipzig
BAUMGARTNER = "9001"  # not in the squad


@pytest.fixture(params=["sqlite", "postgres"])
async def sessions(request, tmp_path):
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'news.db'}"
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


async def seed_players(sessions) -> None:
    async with sessions.begin() as session:
        for id_, name in (("43", "Leipzig"), ("3", "Dortmund"), ("29", "Paderborn")):
            session.add(Team(id=id_, name=name, image=None, updated_at=NOW))
        for id_, name, team in (
            (VANDEVOORDT, "Vandevoordt", "43"),
            (BAUMGARTNER, "Baumgartner", "43"),
            ("9002", "Can", "3"),
        ):
            session.add(
                Player(
                    id=id_,
                    name=name,
                    team_id=team,
                    position=1,
                    status=0,
                    market_value=1_000_000,
                    image=None,
                    updated_at=NOW,
                )
            )


def feed_xml(items: list) -> bytes:
    """An RSS document with the given items (title, description, link)."""
    entries = "".join(
        f"<item><title>{title}</title><description>{text}</description><link>{link}</link>"
        f"<guid>{link}</guid><pubDate>Tue, 06 Oct 2026 09:{i:02d}:00 GMT</pubDate></item>"
        for i, (title, text, link) in enumerate(items)
    )
    return f"<rss><channel><title>t</title>{entries}</channel></rss>".encode()


# ---------------------------------------------------------------------------- feed


def test_parse_feed():
    feed = parse_feed(FEED)
    assert [i.title for i in feed.items] == [
        "Torwart fehlt mehrere Wochen: Leipzig muss umplanen",
        "Der nächste Schritt zum Comeback",
        "Wer wechselt wohin? Der Transferticker",
    ]
    assert (feed.broken, feed.total) == (1, 4)  # the item without date
    first, second = feed.items[0], feed.items[1]
    assert first.guid == "https://www.kicker.de/torwart-fehlt-1000001/artikel#omrss"
    assert first.categories == ("Bundesliga", "Fußball", "Vandevoordt", "RB Leipzig")
    assert first.published_at == datetime(2026, 10, 6, 9, 30, tzinfo=UTC)
    assert second.published_at == datetime(2026, 10, 5, 14, 0, tzinfo=UTC)  # +0200 → UTC
    assert "drei Wochen" in first.teaser


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"<html><body>Wartung</body></html>", "unbekanntes Format"),
        (b"kein xml", "kein gültiges XML"),
        (b"<rss><channel><title>leer</title></channel></rss>", "ohne Einträge"),
        (b"<rss><channel><item><title>x</title></item></channel></rss>", "kein Eintrag lesbar"),
        (
            b'<!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel>&a;</channel></rss>',
            "kein gültiges XML",
        ),
    ],
)
def test_parse_feed_rejects_changed_formats(content, message):
    with pytest.raises(FeedError, match=message):
        parse_feed(content)


def test_only_http_links_are_accepted():
    feed = parse_feed(
        feed_xml([("Gut", "x", "https://www.kicker.de/a"), ("Böse", "x", "javascript:alert(1)")])
    )
    assert [i.title for i in feed.items] == ["Gut"]
    assert feed.broken == 1


async def test_fetch_sends_own_user_agent():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=FEED)

    feed = KickerFeed(transport=httpx.MockTransport(handler))
    assert len((await feed.fetch()).items) == 3
    assert requests[0].headers["user-agent"] == USER_AGENT
    await feed.aclose()


async def test_fetch_errors():
    feed = KickerFeed(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(FeedError, match="HTTP 503"):
        await feed.fetch()
    await feed.aclose()

    def broken(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    feed = KickerFeed(transport=httpx.MockTransport(broken))
    with pytest.raises(FeedError, match="nicht erreichbar"):
        await feed.fetch()
    await feed.aclose()


async def test_duplicate_guids_in_one_feed(sessions):
    item = parse_feed(FEED).items[0]
    assert await store_items(sessions, [item, item], NOW) == 1
    assert await store_items(sessions, [item], NOW) == 0


# ---------------------------------------------------------------------------- matching

PLAYERS = [
    KnownPlayer("1", "Baumgartner", "43"),
    KnownPlayer("2", "S. Michel", "29"),
    KnownPlayer("3", "Tiago Tomás", "9"),
    KnownPlayer("4", "Müller", "2"),
    KnownPlayer("5", "Müller", "2"),
    KnownPlayer("6", "El Mala", "28"),
    KnownPlayer("7", "Møller Wolfe", "5"),
    KnownPlayer("8", "Fábio Silva", "3"),
    KnownPlayer("9", "Heuer Fernandes", "6"),
    KnownPlayer("10", "Sambi Lokonga", "6"),
    KnownPlayer("11", "Becker", "8"),
    KnownPlayer("12", "Becker", "8"),
    KnownPlayer("13", "Jan Becker", "30"),
]


@pytest.mark.parametrize(
    ("name", "team", "expected"),
    [
        ("Christoph Baumgartner", "43", "1"),
        ("Baumgartner", "43", "1"),
        ("Baumgartner", "3", None),  # wrong club
        ("Baumgartner", None, None),  # club unknown: never guess
        ("Sven Michel", "29", "2"),
        ("Michel", "29", "2"),
        ("Thomas Michel", "29", None),  # initial doesn't fit
        ("Tiago Tomas", "9", "3"),
        ("Thomas Müller", "2", None),  # two Müllers: ambiguous
        ("Said El Mala", "28", "6"),
        ("Mala", "28", "6"),  # unique in the club
        # Live cases from LigaInsider (08.10.2026)
        ("Silva", "3", "8"),
        ("Fernandes", "6", "9"),
        ("Sambi", "6", "10"),
        ("T. Becker", "8", None),  # two players "Becker" at Schalke
        ("T. Becker", "30", None),  # initial doesn't fit "Jan Becker"
        ("J. Becker", "30", "13"),
        ("Jacob Moller Wolfe", "5", "7"),
    ],
)
def test_match_player(name, team, expected):
    assert match_player(name, team, PLAYERS) == expected


def test_names_with_apostrophes():
    players = [KnownPlayer("1", "N'Dicka", "4")]
    assert match_player("Evan N’Dicka", "4", players) == "1"


def test_normalize():
    assert normalize("  Fábio  Silva-Junior ") == ("fabio", "silva", "junior")
    assert normalize("Ødegaard") == ("odegaard",)


# ---------------------------------------------------------------------------- extraction


def report(nr: int, name: str, club: str, art: str, status: str, **extra) -> dict:
    return {
        "nr": nr,
        "spieler": name,
        "verein": club,
        "art": art,
        "status": status,
        "ausfall": extra.get("ausfall"),
        "rueckkehr": extra.get("rueckkehr"),
    }


def vandevoordt_out(nr: int) -> dict:
    return report(
        nr, "Maarten Vandevoordt", "Leipzig", "verletzung", "faellt_aus", ausfall="rund drei Wochen"
    )


# In feed order (extract_batch on the parsed feed).
VANDEVOORDT_OUT = vandevoordt_out(1)
EXTRACTION = {
    "meldungen": [
        VANDEVOORDT_OUT,
        report(2, "Christoph Baumgartner", "Leipzig", "rueckkehr", "aufbautraining"),
        report(9, "Erfunden", "Leipzig", "verletzung", "faellt_aus"),  # no such item
        report(0, "Vandevoordt", "Leipzig", "verletzung", "faellt_aus"),  # no such item
        report(3, "Max Kruse", "Leipzig", "verletzung", "faellt_aus"),  # not in that item
    ]
}


async def test_extract_batch_keeps_only_reports_backed_by_the_item(sessions):
    fake = FakeLlm(EXTRACTION)
    llm = make_llm(sessions, fake, FakeClock(NOW))
    items = parse_feed(FEED).items
    results, dropped = await extract_batch(llm, items, ["Dortmund", "Leipzig"])
    assert [(item.title, r.spieler) for item, r in results] == [
        ("Torwart fehlt mehrere Wochen: Leipzig muss umplanen", "Maarten Vandevoordt"),
        ("Der nächste Schritt zum Comeback", "Christoph Baumgartner"),
    ]
    assert dropped == 3
    schema = fake.requests[0]["response_format"]["json_schema"]["schema"]
    club = schema["properties"]["meldungen"]["items"]["properties"]["verein"]
    assert club["enum"] == ["Dortmund", "Leipzig", OTHER_CLUB]
    prompt = fake.requests[0]["messages"][1]["content"]
    assert "[1] Torwart fehlt mehrere Wochen" in prompt
    assert "Schlagwörter: Bundesliga, Fußball, Vandevoordt, RB Leipzig" in prompt
    await llm.aclose()


def test_prompt_contains_only_public_news():
    prompt = build_prompt(parse_feed(FEED).items, ["Leipzig"])
    assert prompt.startswith("Vereinsliste: Leipzig")
    assert prompt.count("\n\n[") == 3


# ---------------------------------------------------------------------------- job

# The job hands pending items to the model oldest first: 1 transfer ticker, 2 Baumgartner,
# 3 Vandevoordt.
JOB_OUT = vandevoordt_out(3)
JOB_EXTRACTION = {
    "meldungen": [
        JOB_OUT,
        report(2, "Christoph Baumgartner", "Leipzig", "rueckkehr", "aufbautraining"),
        report(9, "Erfunden", "Leipzig", "verletzung", "faellt_aus"),
        report(0, "Vandevoordt", "Leipzig", "verletzung", "faellt_aus"),
        report(1, "Maarten Vandevoordt", "Leipzig", "verletzung", "faellt_aus"),  # wrong item
    ]
}


@pytest.fixture
async def news_ctx(sessions):
    from app.worker.jobs import JobContext

    await seed_players(sessions)
    clock = FakeClock(NOW)
    client = make_client(fake_with_team_data(), clock=clock, serve_stale=False)
    kv = KeyValueStore(sessions)
    telegram = FakeTelegram()
    llm_fake = FakeLlm()
    feed_responses = [httpx.Response(200, content=FEED)]
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
        news_feed=KickerFeed(transport=httpx.MockTransport(lambda r: feed_responses[0])),
    )
    llm = ctx.llm = make_llm(sessions, llm_fake, clock)
    ctx.telegram, ctx.llm_fake, ctx.feed_responses = telegram, llm_fake, feed_responses
    yield ctx
    await client.aclose()
    await llm.aclose()
    await ctx.news_feed.aclose()
    await notifier.aclose()


async def count(sessions, model, *where) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


def use_feed(ctx, content: bytes) -> None:
    ctx.feed_responses[0] = httpx.Response(200, content=content)


async def test_collect_news_extracts_matches_and_notifies_once(news_ctx):
    ctx = news_ctx
    ctx.llm_fake.answers.append(JOB_EXTRACTION)
    summary = await collect_news(ctx)
    assert summary == (
        "3 neue Meldungen, 2 Angaben (2 Spielern zugeordnet), 3 verworfen, "
        "1 zum eigenen Kader gemeldet"
    )
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_not(None)) == 3
    reports = await reports_for_players(
        ctx.sessions, [VANDEVOORDT, BAUMGARTNER], NOW - timedelta(days=7)
    )
    assert reports[VANDEVOORDT][0].status_label == "fällt aus"
    assert reports[VANDEVOORDT][0].absence == "rund drei Wochen"
    assert reports[BAUMGARTNER][0].badge == "rehab"
    # Only the squad player is reported, with a link to the player page.
    [text] = ctx.telegram.texts
    assert text.startswith("<b>Testliga: Meldungen zu deinem Kader</b>")
    assert f'<a href="https://kbh.test/spieler/{VANDEVOORDT}">Vandevoordt</a>' in text
    assert "fällt aus (Verletzung: rund drei Wochen)" in text
    assert "Baumgartner" not in text
    # All handled reports are marked, also the one about a player not in the squad.
    assert await count(ctx.sessions, NewsReport, NewsReport.notified_at.is_(None)) == 0

    # Second run: nothing new, no request to the model, no message.
    use_feed(ctx, FEED)
    assert await collect_news(ctx) == "0 neue Meldungen, nichts auszuwerten"
    assert len(ctx.llm_fake.requests) == 1
    assert len(ctx.telegram.texts) == 1
    status = await ctx.kv.get(FEED_STATUS_KEY)
    assert (status["ok"], status["total"], status["broken"]) == (True, 4, 1)
    assert await news_problem(ctx.kv, NOW) is None


async def test_telegram_text_from_the_model_is_escaped(news_ctx):
    ctx = news_ctx
    out = dict(JOB_OUT, ausfall="< 3 Wochen & Zerrung", rueckkehr="<b>bald</b>")
    ctx.llm_fake.answers.append({"meldungen": [out]})
    await collect_news(ctx)
    [text] = ctx.telegram.texts
    assert "(Verletzung: &lt; 3 Wochen &amp; Zerrung, &lt;b&gt;bald&lt;/b&gt;)" in text


async def test_failed_telegram_send_is_retried_next_run(news_ctx):
    ctx = news_ctx
    ctx.llm_fake.answers.append({"meldungen": [JOB_OUT]})
    ctx.telegram.fail = True
    with pytest.raises(TelegramError):
        await collect_news(ctx)
    assert await count(ctx.sessions, NewsReport, NewsReport.notified_at.is_(None)) == 1
    ctx.telegram.fail = False
    use_feed(ctx, FEED)
    assert await collect_news(ctx) == "0 neue Meldungen, nichts auszuwerten, 1 gemeldet"
    assert len(ctx.telegram.texts) == 1


def many_items(n: int) -> bytes:
    """Vandevoordt first, then n - 1 articles about unknown players."""
    items = [("Torwart fehlt", "Maarten Vandevoordt fällt aus.", "https://www.kicker.de/v")]
    items += [
        (f"Meldung {i}", f"Spieler{i} trainiert.", f"https://www.kicker.de/{i}")
        for i in range(n - 1)
    ]
    return feed_xml(items)


async def test_later_batch_failing_still_alerts_earlier_batches(news_ctx):
    ctx = news_ctx
    use_feed(ctx, many_items(13))  # batches of 12 and 1
    ctx.llm_fake.answers += [
        {"meldungen": [VANDEVOORDT_OUT]},
        httpx.Response(503, json={"message": "busy"}),
    ]
    with pytest.raises(Exception, match="HTTP 503"):
        await collect_news(ctx)
    assert len(ctx.telegram.texts) == 1  # batch 1 was alerted before the job failed
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None)) == 1
    status = await ctx.kv.get(EXTRACT_STATUS_KEY)
    assert (status["state"], status["pending"]) == ("failed", 1)
    assert await news_problem(ctx.kv, NOW) == (
        "Auswertung der Meldungen zuletzt fehlgeschlagen (KI-Anbieter antwortet mit HTTP 503)."
    )


async def test_second_batch_maps_numbers_within_the_batch(news_ctx):
    ctx = news_ctx
    use_feed(ctx, many_items(13))
    last = parse_feed(many_items(13)).items[-1]
    name = last.teaser.split()[0]  # "Spieler11"
    ctx.llm_fake.answers += [
        {"meldungen": []},
        {"meldungen": [report(1, name, "Leipzig", "rueckkehr", "aufbautraining")]},
    ]
    summary = await collect_news(ctx)
    assert "1 Angaben (0 Spielern zugeordnet)" in summary
    async with ctx.sessions() as session:
        saved = (await session.execute(select(NewsReport))).scalar_one()
    assert saved.news_guid == last.guid


async def test_truncated_answer_splits_the_batch(news_ctx):
    ctx = news_ctx
    truncated = llm_response('{"meldungen": [', finish="length")
    # 3 items → halves [ticker] and [Baumgartner, Vandevoordt]
    ctx.llm_fake.answers += [truncated, {"meldungen": []}, {"meldungen": [vandevoordt_out(2)]}]
    summary = await collect_news(ctx)
    assert summary.startswith("3 neue Meldungen, 1 Angaben (1 Spielern zugeordnet)")
    assert len(ctx.llm_fake.requests) == 3
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None)) == 0


async def test_schema_errors_give_up_after_some_runs(news_ctx):
    ctx = news_ctx
    for run in range(1, MAX_ATTEMPTS + 1):
        ctx.llm_fake.answers.append(llm_response("kein json"))
        use_feed(ctx, FEED)
        summary = await collect_news(ctx)
        pending = await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None))
        if run < MAX_ATTEMPTS:
            assert pending == 3 and "aufgegeben" not in summary
        else:
            assert pending == 0 and summary.endswith("3 Meldungen aufgegeben")
    assert "3 Meldungen ließen sich nicht auswerten." in await news_problem(ctx.kv, NOW)
    # The hint stays on later runs while the given-up items are recent.
    use_feed(ctx, FEED)
    await collect_news(ctx)
    assert "3 Meldungen ließen sich nicht auswerten." in await news_problem(ctx.kv, NOW)


async def test_collect_news_without_ai_only_stores_and_says_so(news_ctx):
    ctx = news_ctx
    ctx.llm = None
    assert await collect_news(ctx) == "3 neue Meldungen, KI aus"
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None)) == 3
    assert (
        await news_problem(ctx.kv, NOW)
        == "KI-Ausfallanalyse aus; Originalartikel bleiben verfügbar."
    )


async def test_waits_for_clubs_and_players(news_ctx):
    ctx = news_ctx
    async with ctx.sessions.begin() as session:
        await session.execute(Player.__table__.delete())
    summary = await collect_news(ctx)
    assert summary == "3 neue Meldungen, Auswertung wartet auf den Spieler-Abgleich"
    assert ctx.llm_fake.requests == []
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None)) == 3


async def test_old_news_are_not_sent_to_the_model(news_ctx):
    ctx = news_ctx
    ctx.clock.advance(days=5)
    assert await collect_news(ctx) == "3 neue Meldungen, nichts auszuwerten"
    assert ctx.llm_fake.requests == []


async def test_ai_limit_keeps_items_pending_and_notifies_per_period(news_ctx):
    ctx = news_ctx
    ctx.llm = make_llm(ctx.sessions, ctx.llm_fake, ctx.clock, calls_per_day=1)
    async with ctx.sessions.begin() as session:  # today's single call is already used
        session.add(AiUsage(at=NOW, task="news", model="small", status="ok"))
    summary = await collect_news(ctx)
    assert "KI pausiert (Tageslimit von 1 KI-Aufrufen erreicht)" in summary
    assert await count(ctx.sessions, NewsItem, NewsItem.processed_at.is_(None)) == 3
    assert ctx.llm_fake.requests == []
    [text] = ctx.telegram.texts
    assert text.startswith("<b>KI pausiert bis morgen</b>")
    assert await ctx.kv.get(AI_PAUSE_KEY) == "day:2026-10-06"
    assert (await news_problem(ctx.kv, NOW)).startswith("KI pausiert (Tageslimit")
    use_feed(ctx, FEED)
    await collect_news(ctx)
    assert len(ctx.telegram.texts) == 1  # once per pause period
    # A monthly pause later the same month is reported again.
    ctx.llm = make_llm(ctx.sessions, ctx.llm_fake, ctx.clock, budget_usd_per_month=0.001)
    ctx.clock.advance(days=1)
    use_feed(ctx, FEED)
    await collect_news(ctx)
    assert ctx.telegram.texts[-1].startswith("<b>KI pausiert bis zum Monatsersten</b>")
    assert await ctx.kv.get(AI_PAUSE_KEY) == "month:2026-10"


async def test_quota_exhausted_pauses(news_ctx):
    ctx = news_ctx
    ctx.llm_fake.answers.append(httpx.Response(402, json={"message": "Payment required"}))
    summary = await collect_news(ctx)
    assert "KI pausiert (Guthaben aufgebraucht" in summary
    assert ctx.telegram.texts[-1].startswith("<b>KI pausiert bis zum Monatsersten</b>")


async def test_feed_error_fails_the_job_and_is_shown(news_ctx):
    ctx = news_ctx
    ctx.feed_responses[0] = httpx.Response(500)
    with pytest.raises(FeedError):
        await collect_news(ctx)
    assert (await news_problem(ctx.kv, NOW)) == (
        "kicker-Meldungen zuletzt nicht lesbar (kicker-Feed antwortet mit HTTP 500)."
    )


async def test_news_problem_hints():
    kv = MemoryKV()
    assert await news_problem(kv, NOW) == "Noch keine kicker-Meldungen gesammelt."
    old = (NOW - timedelta(hours=30)).isoformat()
    await kv.set(FEED_STATUS_KEY, {"ok": True, "at": old})
    assert "seit über einem Tag nicht aktualisiert" in await news_problem(kv, NOW)
    fresh = NOW.isoformat()
    await kv.set(FEED_STATUS_KEY, {"ok": True, "at": fresh, "total": 20, "broken": 15})
    assert "nur teilweise lesbar (15 von 20" in await news_problem(kv, NOW)
    await kv.set(FEED_STATUS_KEY, {"ok": True, "at": fresh, "total": 20, "broken": 0})
    await kv.set(EXTRACT_STATUS_KEY, {"at": fresh, "state": "ok", "pending": 4, "skipped": 0})
    assert await news_problem(kv, NOW) == "4 Meldungen warten noch auf die Auswertung."
    await kv.set(EXTRACT_STATUS_KEY, {"at": old, "state": "ok", "pending": 0, "skipped": 0})
    assert "Auswertung der Meldungen ist seit über einem Tag" in await news_problem(kv, NOW)


async def test_reprocessing_replaces_reports(news_ctx):
    ctx = news_ctx
    items = parse_feed(FEED).items
    extracted = [(items[0], ExtractedReport.model_validate(VANDEVOORDT_OUT))]
    clubs = {"Leipzig": "43"}
    players = [KnownPlayer(VANDEVOORDT, "Vandevoordt", "43")]
    await store_items(ctx.sessions, items[:1], NOW)
    await save_reports(ctx.sessions, items[:1], extracted, clubs, players, NOW)
    await save_reports(ctx.sessions, items[:1], extracted, clubs, players, NOW)
    assert await count(ctx.sessions, NewsReport) == 1


async def test_newest_report_first(news_ctx):
    ctx = news_ctx
    items = parse_feed(FEED).items
    older = dataclasses.replace(items[0], guid="old", published_at=NOW - timedelta(days=2))
    out = ExtractedReport.model_validate(VANDEVOORDT_OUT)
    back = ExtractedReport.model_validate(
        dict(VANDEVOORDT_OUT, art="rueckkehr", status="einsatzbereit", ausfall=None)
    )
    clubs, players = {"Leipzig": "43"}, [KnownPlayer(VANDEVOORDT, "Vandevoordt", "43")]
    await store_items(ctx.sessions, [older, items[0]], NOW)
    await save_reports(ctx.sessions, [older], [(older, out)], clubs, players, NOW)
    await save_reports(ctx.sessions, items[:1], [(items[0], back)], clubs, players, NOW)
    reports = await reports_for_players(ctx.sessions, [VANDEVOORDT], NOW - timedelta(days=7))
    assert [r.status_label for r in reports[VANDEVOORDT]] == ["wieder einsatzbereit", "fällt aus"]


async def test_known_players_leave_out_departed_players(sessions):
    from app.news.service import known_players

    await seed_players(sessions)
    async with sessions.begin() as session:
        session.add(
            Player(
                id="old",
                name="Vandevoordt",
                team_id="43",
                position=1,
                status=0,
                market_value=1,
                image=None,
                updated_at=NOW - timedelta(days=10),  # left the league
            )
        )
    known = await known_players(sessions)
    assert "old" not in {p.id for p in known}
    # So the current keeper stays unambiguous.
    assert match_player("Maarten Vandevoordt", "43", known) == VANDEVOORDT


def test_tagged_articles_require_unique_player_and_club():
    from app.news.service import tagged_players

    players = [KnownPlayer("1", "Müller", "1"), KnownPlayer("2", "Müller", "2")]
    clubs = {"Bayern": "1", "Mainz": "2"}
    assert [
        p.id for p in tagged_players(["Thomas Müller", "FC Bayern München"], clubs, players)
    ] == ["1"]
    assert tagged_players(["Müller"], clubs, players) == ()
    assert tagged_players(["Müller", "Bayern", "Mainz"], clubs, players) == ()
    assert tagged_players(["Müllerheim", "Bayern"], clubs, players) == ()
    assert tagged_players(["Can", "Bayern"], clubs, [KnownPlayer("3", "Can", "2")]) == ()


async def test_original_articles_work_without_extraction(news_ctx):
    from app.news.service import headlines_for_players

    ctx = news_ctx
    ctx.llm = None
    await collect_news(ctx)
    items = await headlines_for_players(ctx.sessions, [VANDEVOORDT], NOW - timedelta(days=7))
    assert len(items) == 1
    assert items[0].players[0].id == VANDEVOORDT
    assert "Torwart fehlt" in items[0].title
    assert await count(ctx.sessions, NewsReport) == 0
    assert await headlines_for_players(ctx.sessions, ["unknown"], NOW - timedelta(days=7)) == []
    assert await headlines_for_players(ctx.sessions, [VANDEVOORDT], NOW) == []


@pytest.mark.parametrize("hours,expected_calls", [(7, 0), (6, 1), (1, 1), (0, 0), (-1, 0)])
async def test_deadline_news_uses_actual_deadline(news_ctx, hours, expected_calls):
    from app.worker.news_jobs import deadline_news

    ctx = news_ctx
    ctx.llm = None
    # Replace only the market method; RSS and database remain real fixture clients.
    from app.kickbase.client import Fetched
    from app.kickbase.models import Market

    async def market(_):
        return Fetched(
            Market.model_validate({"it": [], "dt": (NOW + timedelta(hours=hours)).isoformat()}),
            NOW,
            False,
        )

    ctx.client.market = market
    attempts = []
    fetch = ctx.news_feed.fetch

    async def tracked_fetch():
        attempts.append(1)
        return await fetch()

    ctx.news_feed.fetch = tracked_fetch
    await deadline_news(ctx)
    await deadline_news(ctx)
    assert len(attempts) == expected_calls
    if expected_calls:
        ctx.clock.advance(minutes=59)
        await deadline_news(ctx)
        assert len(attempts) == 1


async def test_deadline_news_does_not_retry_failed_feed_immediately(news_ctx):
    from app.kickbase.client import Fetched
    from app.kickbase.models import Market
    from app.worker.news_jobs import deadline_news

    ctx = news_ctx

    async def market(_):
        return Fetched(
            Market.model_validate({"it": [], "dt": (NOW + timedelta(hours=2)).isoformat()}),
            NOW,
            False,
        )

    ctx.client.market = market
    ctx.feed_responses[0] = httpx.Response(500)
    with pytest.raises(FeedError):
        await deadline_news(ctx)
    assert await deadline_news(ctx) == "Feed vor weniger als einer Stunde geprüft"


async def test_news_status_without_timezone_is_unknown():
    kv = MemoryKV()
    await kv.set(FEED_STATUS_KEY, {"ok": True, "at": "2026-10-06T12:00:00"})
    assert await news_problem(kv, NOW) == "Status des kicker-Feeds unbekannt."
