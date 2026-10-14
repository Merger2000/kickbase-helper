from dataclasses import replace
from datetime import timedelta

from app.db.models import NewsItem
from app.news.extract import ExtractedReport
from app.news.kicker import parse_feed
from app.news.matching import KnownPlayer
from app.news.service import (
    MAX_REEXTRACTIONS,
    count_pending,
    headlines_for_players,
    mark_notified,
    pending_items,
    record_failed_attempt,
    rematch_reports,
    reports_for_players,
    reports_to_notify,
    save_reports,
    store_items,
)
from app.worker.news_jobs import collect_news
from tests.test_news import (
    FEED,
    JOB_EXTRACTION,
    NOW,
    VANDEVOORDT,
    VANDEVOORDT_OUT,
    seed_players,
    use_feed,
)
from tests.test_news import news_ctx as news_context
from tests.test_news import sessions as news_sessions

news_ctx = news_context
sessions = news_sessions
ITEM = parse_feed(FEED).items[0]
CLUBS = {"Leipzig": "43"}
PLAYERS = [KnownPlayer(VANDEVOORDT, "Vandevoordt", "43")]
OUT = ExtractedReport.model_validate(VANDEVOORDT_OUT)
BACK = ExtractedReport.model_validate(
    dict(VANDEVOORDT_OUT, art="rueckkehr", status="einsatzbereit", ausfall=None)
)


async def article(sessions):
    async with sessions() as session:
        return await session.get(NewsItem, ITEM.guid)


async def test_changed_article_hides_superseded_report_until_matching_revision_saved(sessions):
    await store_items(sessions, [ITEM], NOW)
    await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, PLAYERS, NOW)
    changed = replace(
        ITEM, title="Vandevoordt wieder einsatzbereit", teaser="Der Torwart kehrt zurück."
    )
    assert await store_items(sessions, [changed], NOW + timedelta(hours=1)) == 0
    assert not await reports_for_players(sessions, [VANDEVOORDT], NOW - timedelta(days=7))
    assert await count_pending(sessions, NOW - timedelta(days=3)) == 1
    assert (await article(sessions)).revision == 2
    # A stale model answer does not overwrite the newer text or mark it processed.
    assert await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, PLAYERS, NOW) == []
    assert (await article(sessions)).processed_at is None
    await save_reports(sessions, [changed], [(changed, BACK)], CLUBS, PLAYERS, NOW)
    reports = await reports_for_players(sessions, [VANDEVOORDT], NOW - timedelta(days=7))
    report = reports[VANDEVOORDT][0]
    assert report.revision == 2 and report.status == "einsatzbereit"
    assert report.source_excerpt.startswith(changed.title)
    assert report.updated_at == NOW + timedelta(hours=1)


async def test_cosmetic_metadata_does_not_requeue_or_rejuvenate(sessions):
    await store_items(sessions, [ITEM], NOW)
    await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, PLAYERS, NOW)
    changed = replace(
        ITEM,
        title="  " + ITEM.title + "  ",
        categories=tuple(reversed(ITEM.categories)),
        published_at=NOW + timedelta(days=1),
        link=ITEM.link + "?new=1",
    )
    await store_items(sessions, [changed], NOW + timedelta(days=1))
    row = await article(sessions)
    assert row.revision == 1 and row.updated_at is None and row.reprocess_count == 0
    assert row.published_at == ITEM.published_at and row.link == changed.link
    assert await count_pending(sessions, NOW - timedelta(days=3)) == 0


async def test_pending_revisions_coalesce_and_old_failure_does_not_spend_attempt(sessions):
    await store_items(sessions, [ITEM], NOW)
    changed = replace(ITEM, teaser=ITEM.teaser + " Neues Training.")
    await store_items(sessions, [changed], NOW)
    assert await record_failed_attempt(sessions, [ITEM], NOW) == 0
    row = await article(sessions)
    assert row.attempts == 0 and row.reprocess_count == 0 and row.revision == 2
    assert (await pending_items(sessions, NOW - timedelta(days=3)))[0].teaser == changed.teaser


async def test_reanalysis_budget_is_bounded_and_old_reports_stay_hidden(sessions):
    await store_items(sessions, [ITEM], NOW)
    await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, PLAYERS, NOW)
    for n in range(MAX_REEXTRACTIONS):
        current = replace(ITEM, teaser=f"{ITEM.teaser} Fassung {n}")
        await store_items(sessions, [current], NOW)
        await save_reports(sessions, [current], [(current, OUT)], CLUBS, PLAYERS, NOW)
    beyond = replace(ITEM, teaser="Vandevoordt: weitere neue Fassung")
    await store_items(sessions, [beyond], NOW)
    row = await article(sessions)
    assert row.reprocess_count == MAX_REEXTRACTIONS and row.processed_at is not None
    assert await count_pending(sessions, NOW - timedelta(days=3)) == 0
    assert not await reports_for_players(sessions, [VANDEVOORDT], NOW - timedelta(days=3))


async def test_notification_watermark_survives_revision_with_no_reports(sessions):
    await store_items(sessions, [ITEM], NOW)
    rows = await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, PLAYERS, NOW)
    await mark_notified(sessions, [rows[0].id], NOW)
    empty = replace(ITEM, title="Vandevoordt: Transferthema ohne Ausfall")
    await store_items(sessions, [empty], NOW)
    await save_reports(sessions, [empty], [], CLUBS, PLAYERS, NOW)
    back = replace(ITEM, title="Vandevoordt wieder fit")
    await store_items(sessions, [back], NOW)
    rows = await save_reports(sessions, [back], [(back, BACK)], CLUBS, PLAYERS, NOW)
    assert rows[0].notified_at == NOW
    assert await reports_to_notify(sessions, NOW - timedelta(days=3)) == []


async def test_unmatched_report_can_be_rematched_without_new_extraction(sessions):
    await store_items(sessions, [ITEM], NOW)
    rows = await save_reports(sessions, [ITEM], [(ITEM, OUT)], {}, [], NOW)
    assert rows[0].player_id is None and rows[0].team_id is None
    assert await rematch_reports(sessions, CLUBS, PLAYERS, NOW - timedelta(days=3)) == 1
    assert await rematch_reports(sessions, CLUBS, PLAYERS, NOW - timedelta(days=3)) == 0
    assert (await reports_to_notify(sessions, NOW - timedelta(days=3)))[0].player_id == VANDEVOORDT


async def test_ambiguous_or_superseded_report_is_not_rematched(sessions):
    await store_items(sessions, [ITEM], NOW)
    await save_reports(sessions, [ITEM], [(ITEM, OUT)], CLUBS, [], NOW)
    ambiguous = [*PLAYERS, KnownPlayer("other", "Vandevoordt", "43")]
    assert await rematch_reports(sessions, CLUBS, ambiguous, NOW - timedelta(days=3)) == 0
    await store_items(sessions, [replace(ITEM, title="Geänderter Artikel")], NOW)
    assert await rematch_reports(sessions, CLUBS, PLAYERS, NOW - timedelta(days=3)) == 0


async def test_old_article_with_new_body_has_original_date_and_current_revision(sessions):
    await seed_players(sessions)
    old = replace(ITEM, published_at=NOW - timedelta(days=20))
    await store_items(sessions, [old], NOW - timedelta(days=20))
    await save_reports(sessions, [old], [(old, OUT)], CLUBS, PLAYERS, NOW - timedelta(days=20))
    current = replace(old, title="Vandevoordt: aktuelle Rückkehr", published_at=NOW)
    await store_items(sessions, [current], NOW)
    pending = await pending_items(sessions, NOW - timedelta(days=3))
    assert pending[0].published_at == old.published_at
    headlines = await headlines_for_players(sessions, [VANDEVOORDT], NOW - timedelta(days=3))
    assert headlines[0].revision == 2 and headlines[0].published_at == old.published_at
    await save_reports(sessions, pending, [(pending[0], BACK)], CLUBS, PLAYERS, NOW)
    report = (await reports_for_players(sessions, [VANDEVOORDT], NOW - timedelta(days=3)))[
        VANDEVOORDT
    ][0]
    assert report.current_at == NOW and report.published_at == old.published_at


async def test_worker_reextracts_changed_body_but_does_not_notify_twice(news_ctx):
    ctx = news_ctx
    ctx.llm_fake.answers.append(JOB_EXTRACTION)
    await collect_news(ctx)
    assert len(ctx.telegram.texts) == 1
    modified = FEED.replace(ITEM.title.encode(), b"Vandevoordt wieder einsatzbereit")
    use_feed(ctx, modified)
    ctx.llm_fake.answers.append(
        {
            "meldungen": [
                dict(VANDEVOORDT_OUT, nr=1, art="rueckkehr", status="einsatzbereit", ausfall=None)
            ]
        }
    )
    await collect_news(ctx)
    assert len(ctx.llm_fake.requests) == 2
    assert len(ctx.telegram.texts) == 1
    report = (await reports_for_players(ctx.sessions, [VANDEVOORDT], NOW - timedelta(days=7)))[
        VANDEVOORDT
    ][0]
    assert report.revision == 2 and report.status == "einsatzbereit"
    await collect_news(ctx)
    assert len(ctx.llm_fake.requests) == 2 and len(ctx.telegram.texts) == 1
