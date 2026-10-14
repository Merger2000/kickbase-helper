"""kicker's Bundesliga RSS feed: title, teaser and tags (clubs, player names) of the latest
articles. Fetched a few times a day with one request; a changed format (or an empty feed, which
kicker never serves) raises FeedError instead of silently returning nothing."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

FEED_URL = "https://newsfeed.kicker.de/news/bundesliga"
SOURCE = "kicker"
USER_AGENT = "kickbase-helper/1.0 (privates Dashboard, feste Zeitfenster mit Cache)"
MAX_TEXT = 2000


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class ParsedFeed:
    items: list["FeedItem"]
    broken: int  # entries that could not be read (missing fields, odd links)

    @property
    def total(self) -> int:
        return len(self.items) + self.broken


@dataclass(frozen=True)
class FeedItem:
    guid: str
    link: str
    title: str
    teaser: str
    categories: tuple[str, ...]
    published_at: datetime


class KickerFeed:
    def __init__(
        self, url: str = FEED_URL, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = url
        self._http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> ParsedFeed:
        try:
            response = await self._http.get(self.url)
        except httpx.HTTPError as exc:
            raise FeedError(f"kicker-Feed nicht erreichbar ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise FeedError(f"kicker-Feed antwortet mit HTTP {response.status_code}")
        return parse_feed(response.content)


def parse_feed(content: bytes) -> ParsedFeed:
    try:
        root = ElementTree.fromstring(content)
    except (ParseError, DefusedXmlException) as exc:
        raise FeedError(f"kicker-Feed ist kein gültiges XML ({type(exc).__name__})") from None
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedError("kicker-Feed hat ein unbekanntes Format (kein RSS-Channel)")
    elements = channel.findall("item")
    items, broken = [], 0
    for element in elements:
        item = _item(element)
        if item is None:
            broken += 1
        else:
            items.append(item)
    if not elements:
        raise FeedError("kicker-Feed ohne Einträge, Format geändert?")
    if not items:
        raise FeedError("kicker-Feed: kein Eintrag lesbar, Format geändert?")
    if broken:
        logger.warning("kicker-Feed: %d von %d Einträgen unvollständig", broken, len(elements))
    return ParsedFeed(items, broken)


def _item(element: Element) -> FeedItem | None:
    title = _text(element, "title")
    link = _text(element, "link")
    guid = _text(element, "guid") or link
    published = _text(element, "pubDate")
    if not (title and link and guid and published):
        return None
    if not link.startswith(("https://", "http://")):
        return None  # the link ends up in an href: never javascript: or similar
    try:
        published_at = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    categories = tuple(
        dict.fromkeys(
            c.text.strip() for c in element.findall("category") if c.text and c.text.strip()
        )
    )
    return FeedItem(
        guid=guid[:512],
        link=link[:1024],
        title=title[:MAX_TEXT],
        teaser=_text(element, "description")[:MAX_TEXT],
        categories=categories,
        published_at=published_at.astimezone(UTC),
    )


def _text(element: Element, tag: str) -> str:
    return " ".join((element.findtext(tag) or "").split())
