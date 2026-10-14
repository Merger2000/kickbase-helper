"""Storage of news items and reports, and the queries for the pages."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import NewsItem, NewsReport, Player, Team
from app.db.stores import KeyValueStore
from app.news.extract import (
    KIND_LABELS,
    OTHER_CLUB,
    STATUS_LABELS,
    ExtractedReport,
    Kind,
    ReportStatus,
)
from app.news.kicker import SOURCE, FeedItem
from app.news.matching import KnownPlayer, match_player
from app.services.players import DEPARTED_AFTER

FEED_STATUS_KEY = "news.feed"
EXTRACT_STATUS_KEY = "news.extract"
# The feed is read three times a day; older than this means the worker stopped reading it.
FEED_MAX_AGE = timedelta(hours=26)
# An item the model fails on this often (in different runs) is given up.
MAX_ATTEMPTS = 3
BADGES = {
    ReportStatus.OUT: "out",
    ReportStatus.DOUBTFUL: "doubtful",
    ReportStatus.REHAB: "rehab",
    ReportStatus.AVAILABLE: "fit",
}


# Maximum material revisions that can restart extraction per article. Pending updates coalesce.
MAX_REEXTRACTIONS = 3


def _content(item) -> tuple:
    def text(value):
        return " ".join((value or "").split())

    return (
        text(item.title),
        text(item.teaser),
        tuple(sorted({text(c) for c in (item.categories or ())})),
    )


def _item_time():
    return func.coalesce(NewsItem.updated_at, NewsItem.published_at)


async def store_items(
    sessions: async_sessionmaker, items: Sequence[FeedItem], now: datetime
) -> int:
    """Store new articles and bounded material revisions. Dates/links alone aren't new text."""
    if not items:
        return 0
    incoming = {item.guid: item for item in items}
    new = 0
    async with sessions.begin() as session:
        rows = {
            r.guid: r
            for r in await session.scalars(
                select(NewsItem).where(NewsItem.guid.in_(incoming)).with_for_update()
            )
        }
        for guid, item in incoming.items():
            row = rows.get(guid)
            if row is None:
                session.add(
                    NewsItem(
                        guid=guid,
                        source=SOURCE,
                        title=item.title,
                        teaser=item.teaser,
                        link=item.link,
                        categories=list(item.categories),
                        published_at=item.published_at,
                        fetched_at=now,
                    )
                )
                new += 1
                continue
            changed = _content(row) != _content(item)
            row.title, row.teaser, row.link, row.categories = (
                item.title,
                item.teaser,
                item.link,
                list(item.categories),
            )
            row.fetched_at = now
            # Keep the original publication time: RSS re-dating must not revive unchanged news.
            if not changed:
                continue
            row.revision += 1
            row.updated_at = now
            if row.processed_at is None and row.attempts == 0:
                continue  # pending extraction will read the newest body, no extra attempt
            if row.reprocess_count < MAX_REEXTRACTIONS:
                row.reprocess_count += 1
                row.processed_at = None
                row.attempts = 0
            else:
                row.processed_at = now
                row.attempts = MAX_ATTEMPTS  # visible as given up; superseded reports stay hidden
    return new


def _pending(since: datetime):
    return (NewsItem.processed_at.is_(None), _item_time() >= since)


async def pending_items(sessions: async_sessionmaker, since: datetime) -> list[FeedItem]:
    """Recent items the language model hasn't processed yet, oldest first."""
    async with sessions() as session:
        rows = (
            await session.execute(
                select(NewsItem).where(*_pending(since)).order_by(NewsItem.published_at)
            )
        ).scalars()
        return [
            FeedItem(
                guid=r.guid,
                link=r.link,
                title=r.title,
                teaser=r.teaser,
                categories=tuple(r.categories or ()),
                published_at=r.published_at,
            )
            for r in rows
        ]


async def count_pending(sessions: async_sessionmaker, since: datetime) -> int:
    async with sessions() as session:
        return await session.scalar(
            select(func.count()).select_from(NewsItem).where(*_pending(since))
        )


async def count_given_up(sessions: async_sessionmaker, since: datetime) -> int:
    """Recent items given up after MAX_ATTEMPTS failures (processed without reports)."""
    async with sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(NewsItem)
            .where(NewsItem.attempts >= MAX_ATTEMPTS, _item_time() >= since)
        )


async def record_failed_attempt(
    sessions: async_sessionmaker, batch: Sequence[FeedItem], now: datetime
) -> int:
    """Count a failed extraction for these items; items that failed MAX_ATTEMPTS times are
    given up (marked processed without reports). Returns how many were given up."""
    incoming = {i.guid: i for i in batch}
    given_up = 0
    async with sessions.begin() as session:
        rows = await session.scalars(
            select(NewsItem).where(NewsItem.guid.in_(incoming)).with_for_update()
        )
        for row in rows:
            if _content(row) != _content(incoming[row.guid]) or row.processed_at is not None:
                continue  # an older extraction cannot consume the new revision's attempts
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                row.processed_at = now
                given_up += 1
    return given_up


async def club_ids(sessions: async_sessionmaker) -> dict[str, str]:
    """Kickbase club name → team id."""
    async with sessions() as session:
        return {name: id_ for id_, name in await session.execute(select(Team.id, Team.name))}


async def known_players(sessions: async_sessionmaker) -> list[KnownPlayer]:
    """Current Bundesliga players. Players that left keep their old club in the table; they
    would make names ambiguous, so only those seen in the last daily syncs count."""
    async with sessions() as session:
        newest = await session.scalar(select(func.max(Player.updated_at)))
        if newest is None:
            return []
        rows = await session.execute(
            select(Player.id, Player.name, Player.team_id).where(
                Player.updated_at >= newest - DEPARTED_AFTER
            )
        )
        return [KnownPlayer(id_, name, team_id) for id_, name, team_id in rows]


async def save_reports(
    sessions: async_sessionmaker,
    batch: Sequence[FeedItem],
    extracted: Iterable[tuple[FeedItem, ExtractedReport]],
    clubs: dict[str, str],
    players: Sequence[KnownPlayer],
    now: datetime,
) -> list[NewsReport]:
    """Replace reports only for the exact body analysed; notification history survives revisions."""
    rows = []
    incoming = {i.guid: i for i in batch}
    extracted = list(extracted)
    async with sessions.begin() as session:
        articles = {
            r.guid: r
            for r in await session.scalars(
                select(NewsItem).where(NewsItem.guid.in_(incoming)).with_for_update()
            )
        }
        valid = {
            guid: row for guid, row in articles.items() if _content(row) == _content(incoming[guid])
        }
        old = list(await session.scalars(select(NewsReport).where(NewsReport.news_guid.in_(valid))))
        # A legacy report may have been marked before the stable article watermark existed.
        for report in old:
            item = valid[report.news_guid]
            if report.notified_at is not None and item.notification_handled_at is None:
                item.notification_handled_at = report.notified_at
            await session.delete(report)
        for item, report in extracted:
            article = valid.get(item.guid)
            if article is None or _content(item) != _content(incoming[item.guid]):
                continue
            team_id = None if report.verein == OTHER_CLUB else clubs.get(report.verein)
            rows.append(
                NewsReport(
                    news_guid=item.guid,
                    published_at=article.published_at,
                    player_name=report.spieler.strip()[:128],
                    team_id=team_id,
                    player_id=match_player(report.spieler, team_id, players),
                    kind=report.art.value,
                    status=report.status.value,
                    absence=report.ausfall,
                    expected_return=report.rueckkehr,
                    revision=article.revision,
                    team_name=report.verein[:128],
                    source_excerpt=(item.title + " — " + item.teaser)[:280],
                    notified_at=article.notification_handled_at,
                )
            )
        session.add_all(rows)
        for item in valid.values():
            item.processed_at = now
    return rows


async def rematch_reports(
    sessions: async_sessionmaker,
    clubs: dict[str, str],
    players: Sequence[KnownPlayer],
    since: datetime,
) -> int:
    """Retry unresolved current reports after roster sync; no new AI requests or guessing."""
    changed = 0
    async with sessions.begin() as session:
        reports = (
            await session.execute(
                select(NewsReport, NewsItem)
                .join(NewsItem, NewsItem.guid == NewsReport.news_guid)
                .where(
                    NewsReport.player_id.is_(None),
                    NewsReport.revision == NewsItem.revision,
                    _item_time() >= since,
                )
            )
        ).all()
        for report, item in reports:
            team_id = report.team_id or clubs.get(report.team_name)
            player_id = match_player(report.player_name, team_id, players)
            if player_id is not None:
                report.team_id = team_id
                report.player_id = player_id
                report.notified_at = item.notification_handled_at or report.notified_at
                changed += 1
    return changed


async def reports_to_notify(sessions: async_sessionmaker, since: datetime) -> list[NewsReport]:
    """Matched reports the Telegram step hasn't handled yet (also those of earlier runs that
    failed before or while sending)."""
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    select(NewsReport)
                    .join(NewsItem, NewsItem.guid == NewsReport.news_guid)
                    .where(
                        NewsReport.player_id.is_not(None),
                        NewsReport.notified_at.is_(None),
                        _item_time() >= since,
                        NewsReport.revision == NewsItem.revision,
                    )
                    .order_by(_item_time(), NewsReport.id)
                )
            ).scalars()
        )


async def mark_notified(sessions: async_sessionmaker, ids: Sequence[int], now: datetime) -> None:
    if not ids:
        return
    async with sessions.begin() as session:
        guids = list(
            await session.scalars(select(NewsReport.news_guid).where(NewsReport.id.in_(ids)))
        )
        await session.execute(
            update(NewsReport).where(NewsReport.id.in_(list(ids))).values(notified_at=now)
        )
        await session.execute(
            update(NewsItem)
            .where(NewsItem.guid.in_(guids), NewsItem.notification_handled_at.is_(None))
            .values(notification_handled_at=now)
        )


# ---------------------------------------------------------------------------- pages


@dataclass(frozen=True)
class ReportView:
    player_id: str | None
    player_name: str
    kind: str
    status: str
    absence: str | None
    expected_return: str | None
    published_at: datetime
    title: str
    link: str
    revision: int = 1
    updated_at: datetime | None = None
    source_excerpt: str | None = None

    @property
    def current_at(self) -> datetime:
        return self.updated_at or self.published_at

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(Kind(self.kind), self.kind) if self.kind in Kind else self.kind

    @property
    def status_label(self) -> str:
        if self.status in ReportStatus:
            return STATUS_LABELS[ReportStatus(self.status)]
        return self.status

    @property
    def badge(self) -> str:
        return (
            BADGES.get(ReportStatus(self.status), "unknown")
            if self.status in ReportStatus
            else "unknown"
        )

    @classmethod
    def of(
        cls, report: NewsReport, title: str = "", link: str = "", updated_at: datetime | None = None
    ) -> "ReportView":
        return cls(
            player_id=report.player_id,
            player_name=report.player_name,
            kind=report.kind,
            status=report.status,
            absence=report.absence,
            expected_return=report.expected_return,
            published_at=report.published_at,
            title=title,
            link=link,
            revision=report.revision or 1,
            updated_at=updated_at,
            source_excerpt=report.source_excerpt,
        )


def _status_time(status: dict) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(status["at"])
        return parsed if parsed.tzinfo is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _feed_hint(status: dict | None, now: datetime) -> str | None:
    if not status:
        return "Noch keine kicker-Meldungen gesammelt."
    at = _status_time(status)
    if at is None:
        return "Status des kicker-Feeds unbekannt."
    if not status.get("ok"):
        return (
            f"kicker-Meldungen zuletzt nicht lesbar ({status.get('detail', 'unbekannter Fehler')})."
        )
    if now - at > FEED_MAX_AGE:
        return "kicker-Meldungen seit über einem Tag nicht aktualisiert."
    broken, total = status.get("broken", 0), status.get("total", 0)
    if total and broken * 2 > total:
        return f"kicker-Feed nur teilweise lesbar ({broken} von {total} Einträgen unvollständig)."
    return None


def _extract_hint(status: dict | None, now: datetime) -> str | None:
    if not status:
        return None  # the first news run hasn't finished yet; the feed hint covers that
    at = _status_time(status)
    state, detail = status.get("state"), status.get("detail") or ""
    if state == "off":
        return "KI-Ausfallanalyse aus; Originalartikel bleiben verfügbar."
    if at is None or now - at > FEED_MAX_AGE:
        return "Die Auswertung der Meldungen ist seit über einem Tag nicht gelaufen."
    hints = []
    if state == "paused":
        hints.append(f"KI pausiert ({detail}); Meldungen der letzten 3 Tage werden nachgeholt.")
    elif state == "failed":
        hints.append(f"Auswertung der Meldungen zuletzt fehlgeschlagen ({detail}).")
    elif status.get("pending"):
        hints.append(f"{status['pending']} Meldungen warten noch auf die Auswertung.")
    if status.get("skipped"):
        hints.append(f"{status['skipped']} Meldungen ließen sich nicht auswerten.")
    return " ".join(hints) or None


async def news_problem(kv: KeyValueStore, now: datetime) -> str | None:
    """A hint for the pages when the news shown may be incomplete, else None."""
    hints = [
        _feed_hint(await kv.get(FEED_STATUS_KEY), now),
        _extract_hint(await kv.get(EXTRACT_STATUS_KEY), now),
    ]
    return " ".join(h for h in hints if h) or None


async def reports_for_players(
    sessions: async_sessionmaker, player_ids: Iterable[str], since: datetime
) -> dict[str, list[ReportView]]:
    """Reports per player since `since`, newest first."""
    ids = list(set(player_ids))
    if not ids:
        return {}
    async with sessions() as session:
        rows = await session.execute(
            select(NewsReport, NewsItem.title, NewsItem.link, NewsItem.updated_at)
            .join(NewsItem, NewsItem.guid == NewsReport.news_guid)
            .where(
                NewsReport.player_id.in_(ids),
                _item_time() >= since,
                NewsReport.revision == NewsItem.revision,
            )
            .order_by(_item_time().desc(), NewsReport.id.desc())
        )
        result: dict[str, list[ReportView]] = {}
        for report, title, link, updated_at in rows:
            result.setdefault(report.player_id, []).append(
                ReportView.of(report, title, link, updated_at)
            )
        return result


@dataclass(frozen=True)
class Headline:
    guid: str
    title: str
    link: str
    published_at: datetime
    players: tuple[KnownPlayer, ...]
    revision: int = 1
    updated_at: datetime | None = None


def tagged_players(
    categories: Sequence[str], clubs: dict[str, str], players: Sequence[KnownPlayer]
) -> tuple[KnownPlayer, ...]:
    """Only explicit RSS player + club tags, with unique matching across the whole roster.

    Headlines/teasers do not establish availability and never alter the score.
    Missing or ambiguous tags deliberately yield no association.
    """
    from app.news.matching import normalize

    teams = set()
    for category in categories:
        tag = normalize(category)
        for name, team_id in clubs.items():
            club = normalize(name)
            if club and any(tag[i : i + len(club)] == club for i in range(len(tag))):
                teams.add(team_id)
    hits = set()
    for category in categories:
        matches = {
            hit for team in teams if (hit := match_player(category, team, players)) is not None
        }
        # A shared surname tagged with both clubs must not select either player.
        if len(matches) == 1:
            hits.update(matches)
    return tuple(p for p in players if p.id in hits)


async def headlines_for_players(
    sessions: async_sessionmaker, player_ids: Iterable[str], since: datetime
) -> list[Headline]:
    ids = set(player_ids)
    if not ids:
        return []
    clubs = await club_ids(sessions)
    players = await known_players(sessions)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(NewsItem)
                .where(_item_time() >= since)
                .order_by(_item_time().desc(), NewsItem.guid)
            )
        ).scalars()
        result = []
        for row in rows:
            relevant = tuple(
                p for p in tagged_players(row.categories or (), clubs, players) if p.id in ids
            )
            if relevant:
                result.append(
                    Headline(
                        row.guid,
                        row.title,
                        row.link,
                        row.published_at,
                        relevant,
                        row.revision,
                        row.updated_at,
                    )
                )
        return result


async def feed_checked_at(kv: KeyValueStore) -> datetime | None:
    status = await kv.get(FEED_STATUS_KEY)
    return _status_time(status) if status else None
