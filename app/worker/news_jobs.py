"""Worker jobs around news and the language model: kicker news → extracted absences →
Telegram alerts about own players, and the morning briefing.

Every exit of `collect_news` leaves a status for the pages (feed and extraction), so missing
news never look like "no news". Reports are alerted from a queue (`notified_at`), so a failure
after saving them only delays the Telegram message to the next run.
"""

import html
import logging
from datetime import timedelta

from app.db.models import NewsReport
from app.llm.errors import (
    LlmBadOutput,
    LlmError,
    LlmLimitReached,
    LlmQuotaExhausted,
    LlmTruncated,
)
from app.news.extract import BATCH_SIZE, extract_batch
from app.news.kicker import FeedError, FeedItem
from app.news.service import (
    EXTRACT_STATUS_KEY,
    FEED_STATUS_KEY,
    ReportView,
    club_ids,
    count_given_up,
    count_pending,
    known_players,
    mark_notified,
    news_problem,
    pending_items,
    record_failed_attempt,
    rematch_reports,
    reports_to_notify,
    save_reports,
    store_items,
)
from app.services.briefing import (
    Briefing,
    collect_briefing_data,
    compose_briefing,
    write_briefing,
)
from app.worker.jobs import JobContext

logger = logging.getLogger(__name__)

# News older than this are stored but no longer sent to the language model.
NEWS_LOOKBACK = timedelta(days=3)
AI_PAUSE_KEY = "ai.pause_notified"
BRIEFING_KEY = "briefing.last_day"
MESSAGE_LIMIT = 3500  # Telegram allows 4096; cut by whole lines well before


async def notify_ai_pause(ctx: JobContext, error: LlmLimitReached | LlmQuotaExhausted) -> None:
    """Tell once per pause period (day or month) that the AI pauses; everything else runs."""
    period = error.period
    stamp = f"{period}:{ctx.today():%Y-%m-%d}" if period == "day" else f"month:{ctx.today():%Y-%m}"
    if ctx.notifier is None or await ctx.kv.get(AI_PAUSE_KEY) == stamp:
        return
    until = "morgen" if period == "day" else "zum Monatsersten"
    await ctx.notifier.send(
        f"<b>KI pausiert bis {until}</b>\n{html.escape(str(error))}\n"
        "Meldungen werden danach nachgeholt (bis 3 Tage zurück), das Briefing kommt bis dahin "
        "ohne KI. Alles andere läuft weiter."
    )
    await ctx.kv.set(AI_PAUSE_KEY, stamp)


async def _set_extract_status(
    ctx: JobContext, state: str, detail: str = "", pending: int = 0, skipped: int = 0
) -> None:
    await ctx.kv.set(
        EXTRACT_STATUS_KEY,
        {
            "at": ctx.clock().isoformat(),
            "state": state,
            "detail": detail,
            "pending": pending,
            "skipped": skipped,
        },
    )


async def collect_news(ctx: JobContext) -> str:
    """kicker feed → new items stored → the language model extracts absences and returns →
    Telegram message for reports about own players."""
    if ctx.news_feed is None:
        return "News aus"
    now = ctx.clock()
    try:
        feed = await ctx.news_feed.fetch()
    except FeedError as exc:
        await ctx.kv.set(FEED_STATUS_KEY, {"ok": False, "at": now.isoformat(), "detail": str(exc)})
        raise
    new = await store_items(ctx.sessions, feed.items, now)
    await ctx.kv.set(
        FEED_STATUS_KEY,
        {"ok": True, "at": now.isoformat(), "total": feed.total, "broken": feed.broken},
    )
    summary = f"{new} neue Meldungen"
    clubs = await club_ids(ctx.sessions)
    players = await known_players(ctx.sessions)
    since = now - NEWS_LOOKBACK
    rematched = await rematch_reports(ctx.sessions, clubs, players, since)
    if rematched:
        summary += f", {rematched} nachträglich zugeordnet"
    if ctx.llm is None:
        await _set_extract_status(ctx, "off")
        return f"{summary}, KI aus"

    if not clubs or not players:
        # Without them every report would be stored without club and player for good.
        waiting = await count_pending(ctx.sessions, since)
        await _set_extract_status(
            ctx, "failed", "Vereine und Spieler noch nicht abgeglichen", pending=waiting
        )
        return f"{summary}, Auswertung wartet auf den Spieler-Abgleich"

    queue = [batch for batch in _batches(await pending_items(ctx.sessions, since))]
    if not queue:
        await _set_extract_status(ctx, "ok", skipped=await count_given_up(ctx.sessions, since))
        sent = await notify_reports(ctx)
        return f"{summary}, nichts auszuwerten" + (f", {sent} gemeldet" if sent else "")

    saved: list[NewsReport] = []
    dropped = skipped = 0
    paused: LlmLimitReached | LlmQuotaExhausted | None = None
    failure: LlmError | None = None
    while queue:
        batch = queue.pop(0)
        try:
            extracted, rejected = await extract_batch(ctx.llm, batch, sorted(clubs))
        except (LlmLimitReached, LlmQuotaExhausted) as exc:
            paused = exc  # the rest stays pending until the AI is back
            break
        except LlmTruncated as exc:
            if len(batch) > 1:  # too much for one answer: try the halves
                middle = len(batch) // 2
                queue[:0] = [batch[:middle], batch[middle:]]
                continue
            logger.warning("Meldung zu lang für eine Antwort: %s", exc)
            skipped += await record_failed_attempt(ctx.sessions, batch, ctx.clock())
            continue
        except LlmBadOutput as exc:
            logger.warning("%d Meldungen nicht ausgewertet: %s", len(batch), exc)
            skipped += await record_failed_attempt(ctx.sessions, batch, ctx.clock())
            continue
        except LlmError as exc:
            failure = exc  # temporary or rejected: stays pending, the job is marked failed
            break
        dropped += rejected
        saved += await save_reports(ctx.sessions, batch, extracted, clubs, players, ctx.clock())

    pending = await count_pending(ctx.sessions, since)
    # Given-up items of the last days, not just of this run: the hint stays while they matter.
    given_up = await count_given_up(ctx.sessions, since)
    if paused is not None:
        await _set_extract_status(ctx, "paused", str(paused), pending, given_up)
    elif failure is not None:
        await _set_extract_status(ctx, "failed", str(failure), pending, given_up)
    else:
        await _set_extract_status(ctx, "ok", pending=pending, skipped=given_up)

    matched = sum(1 for r in saved if r.player_id)
    summary += f", {len(saved)} Angaben ({matched} Spielern zugeordnet)"
    if dropped:
        summary += f", {dropped} verworfen"
    if skipped:
        summary += f", {skipped} Meldungen aufgegeben"
    # Alerts for everything saved so far, also when the loop stopped early.
    sent = await notify_reports(ctx)
    if sent:
        summary += f", {sent} zum eigenen Kader gemeldet"
    if paused is not None:
        logger.warning("News-Auswertung pausiert: %s", paused)
        await notify_ai_pause(ctx, paused)
        summary += f", KI pausiert ({paused})"
    if failure is not None:
        raise failure
    return summary


def _batches(items: list[FeedItem]) -> list[list[FeedItem]]:
    return [items[start : start + BATCH_SIZE] for start in range(0, len(items), BATCH_SIZE)]


async def notify_reports(ctx: JobContext) -> int:
    """Send unsent reports about own players; mark all handled reports only after success,
    so a failed run is retried. Reports about other players are marked without a message."""
    reports = await reports_to_notify(ctx.sessions, ctx.clock() - NEWS_LOOKBACK)
    if not reports:
        return 0
    own: list[NewsReport] = []
    if ctx.notifier is not None:
        league = await ctx.league.get()
        squad = {p.id: p for p in (await ctx.client.squad(league.id)).data.players}
        own = [r for r in reports if r.player_id in squad]
        if own:
            await ctx.notifier.send(_own_reports_message(ctx, league.name, own, squad))
    await mark_notified(ctx.sessions, [r.id for r in reports], ctx.clock())
    return len(own)


def _own_reports_message(ctx: JobContext, league_name: str, reports, squad) -> str:
    lines = [f"<b>{html.escape(league_name)}: Meldungen zu deinem Kader</b>"]
    for index, report in enumerate(reports):
        view = ReportView.of(report)
        player = squad[report.player_id]
        details = ", ".join(x for x in (report.absence, report.expected_return) if x)
        url = f"{ctx.public_url}/spieler/{player.id}"
        line = f'<a href="{html.escape(url)}">{html.escape(player.name)}</a>: {view.status_label}'
        line += f" ({view.kind_label}{': ' + html.escape(details) if details else ''})"
        if sum(len(x) + 1 for x in lines) + len(line) > MESSAGE_LIMIT:
            lines.append(f"… und {len(reports) - index} weitere")
            break
        lines.append(line)
    return "\n".join(lines)


async def daily_briefing(ctx: JobContext) -> str:
    """Morning briefing via Telegram: by the language model if possible, else plain. Only with
    a configured AI – without it there was never a briefing, and the dashboard covers it."""
    if ctx.notifier is None or ctx.oldb is None:
        return "Briefing aus (Telegram nicht konfiguriert)"
    if ctx.llm is None:
        return "Briefing aus (KI nicht konfiguriert)"
    today = ctx.today().isoformat()
    if await ctx.kv.get(BRIEFING_KEY) == today:
        return "Briefing heute schon gesendet"
    league = await ctx.league.get()
    now = ctx.clock()
    data = await collect_briefing_data(ctx.client, ctx.oldb, ctx.sessions, league, str(ctx.tz), now)
    paused = None
    try:
        briefing = await write_briefing(ctx.llm, data)
    except (LlmLimitReached, LlmQuotaExhausted) as exc:
        paused = exc
        briefing = Briefing(data.fallback_lines, False, f"KI pausiert: {exc}")
    except LlmError as exc:
        logger.warning("Briefing ohne KI: %s", exc)
        briefing = Briefing(data.fallback_lines, False, f"KI-Fehler: {exc}")
    notes = list(data.notes)
    if hint := await news_problem(ctx.kv, now):
        notes.append(f"Meldungen: {hint}")
    link = f"{ctx.public_url}/empfehlungen"
    await ctx.notifier.send(compose_briefing(league.name, briefing, notes, link))
    await ctx.kv.set(BRIEFING_KEY, today)
    if paused is not None:
        await notify_ai_pause(ctx, paused)
    return f"Briefing gesendet ({briefing.note})"


async def deadline_news(ctx: JobContext) -> str:
    """Hourly check in a fixed window; fetch only within six hours of the real deadline.

    Existing API cache and job lock apply. Feed failures also count as attempts so an
    outage is not retried aggressively. Scheduled news jobs remain the baseline.
    """
    if ctx.news_feed is None:
        return "News aus"
    now = ctx.clock()
    league = await ctx.league.get()
    market = await ctx.client.market(league.id)
    if market.stale or market.data.deadline is None:
        return "Keine aktuelle Deadline"
    remaining = market.data.deadline - now
    if not timedelta(0) < remaining <= timedelta(hours=6):
        return "Außerhalb des News-Deadline-Fensters"
    from app.news.service import feed_checked_at

    checked = await feed_checked_at(ctx.kv)
    if checked is not None and now - checked < timedelta(hours=1):
        return "Feed vor weniger als einer Stunde geprüft"
    return await collect_news(ctx)
