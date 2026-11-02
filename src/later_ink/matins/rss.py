"""RSS/Atom as a Matins source.

The one thing this connector must get right is stability of ids: an item that
appears in two consecutive issues is a visible bug, and an item whose id changes
between fetches defeats the dedupe table. Feeds offer no stable id in general —
`guid` is optional, and plenty of publishers leave it out — so the id is derived
from the link, which is the only field that is both stable and present.
"""

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin
from xml.sax.saxutils import quoteattr

import httpx
from lxml import etree
from lxml import html as lxml_html

from ..connectors.base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
)
from ..fetch import fetch_bytes
from . import config, extract
from .db import Feed, MatinsStore

ATOM = "{http://www.w3.org/2005/Atom}"
RSS_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
MEDIA = "{http://search.yahoo.com/mrss/}"

# Feed documents are attacker-influenced (a publisher, or anyone who can get a
# URL into a user's feed list). Entity expansion, DTD loading and network
# fetches during parse are all refused; recover=True still tolerates the
# unclosed tags real feeds ship.
_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
    recover=True,
)

# A feed fetched once per article download would be one request per item on a
# 15-item issue. Ten minutes is long enough to cover a whole issue build and
# short enough that a scheduled run sees fresh items.
_CACHE_TTL = 600.0
_CACHE_MAX = 256


@dataclass(frozen=True)
class FeedItem:
    id: str
    title: str
    link: str
    author: str | None
    summary: str | None
    content_html: str | None
    published: datetime | None
    image_url: str | None


@dataclass(frozen=True)
class ParsedFeed:
    title: str
    items: list[FeedItem]


_cache: OrderedDict[str, tuple[float, ParsedFeed]] = OrderedDict()

# A feed that just failed is not retried for a while: one dead publisher must not
# cost its full timeout on every page that walks the feeds.
_FAILURE_TTL = 300.0
_failures: dict[str, tuple[float, str]] = {}

_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml")

# Enough to keep one slow publisher from delaying a page, few enough to stay a
# polite client when a catalogue has a hundred subscriptions.
_FETCH_CONCURRENCY = 8


def parse_feed(xml: bytes, feed_url: str, feed_id: str = "") -> ParsedFeed:
    """Parse RSS 2.0 / RSS 1.0 / Atom into a flat item list.

    Raises UpstreamError rather than returning empty on an unparseable document:
    "this feed is broken" and "this feed has no items today" are different
    answers, and a scheduled issue should not silently come out empty because a
    URL started serving an error page.
    """
    try:
        root = etree.fromstring(xml, parser=_PARSER)
    except etree.XMLSyntaxError as e:
        raise UpstreamError("That feed is not valid XML") from e
    if root is None:
        raise UpstreamError("That feed is not valid XML")

    if _localname(root) == "feed":
        title, items = _parse_atom(root, feed_url, feed_id)
    elif _localname(root) == "rss" or root.find("channel") is not None:
        title, items = _parse_rss(root, feed_url, feed_id)
    elif _localname(root) == "RDF":
        title, items = _parse_rss(root, feed_url, feed_id)
    else:
        raise UpstreamError("That URL is not an RSS or Atom feed")
    return ParsedFeed(title=title, items=items)


def _localname(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_rss(root, feed_url: str, feed_id: str) -> tuple[str, list[FeedItem]]:
    channel = root.find("channel")
    if channel is None:
        channel = root  # RSS 1.0 (RDF) carries items at the top level
    title = _text(channel.find("title")) or feed_url
    items: list[FeedItem] = []
    for item in channel.iter("item"):
        link = _text(item.find("link")) or _guid_link(item)
        if not link:
            continue
        content = _element_html(item.find(RSS_CONTENT)) or _element_html(item.find("description"))
        published = _parse_date(
            _text(item.find("pubDate")) or _text(item.find(DC_DATE))
        )
        items.append(
            FeedItem(
                id=_item_id(feed_id, link),
                title=_text(item.find("title")) or link,
                link=link,
                author=_text(item.find(DC_CREATOR)) or _text(item.find("author")),
                summary=_summary(content),
                content_html=content,
                published=published,
                image_url=_rss_image(item),
            )
        )
    return title, items


def _parse_atom(root, feed_url: str, feed_id: str) -> tuple[str, list[FeedItem]]:
    title = _text(root.find(f"{ATOM}title")) or feed_url
    items: list[FeedItem] = []
    for entry in root.findall(f"{ATOM}entry"):
        link = _atom_link(entry)
        if not link:
            continue
        content = _element_html(entry.find(f"{ATOM}content")) or _element_html(
            entry.find(f"{ATOM}summary")
        )
        published = _parse_date(
            _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
        )
        author_el = entry.find(f"{ATOM}author/{ATOM}name")
        items.append(
            FeedItem(
                id=_item_id(feed_id, link),
                title=_text(entry.find(f"{ATOM}title")) or link,
                link=link,
                author=_text(author_el),
                summary=_summary(content),
                content_html=content,
                published=published,
                image_url=None,
            )
        )
    return title, items


def _item_id(feed_id: str, link: str) -> str:
    """Stable, path-safe id. The feed prefix keeps two feeds that carry the same
    link (an aggregator and the original) from colliding in the dedupe table."""
    digest = hashlib.sha1(link.encode()).hexdigest()[:16]
    return f"{feed_id}-{digest}" if feed_id else digest


def _guid_link(item) -> str:
    guid = item.find("guid")
    if guid is None:
        return ""
    if guid.get("isPermaLink", "true").lower() == "false":
        return ""
    return _text(guid)


def _atom_link(entry) -> str:
    fallback = ""
    for link in entry.findall(f"{ATOM}link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "alternate").lower()
        media = (link.get("type") or "").lower()
        if rel == "alternate" and (not media or "html" in media):
            return href
        fallback = fallback or href
    return fallback


def _rss_image(item) -> str | None:
    enclosure = item.find("enclosure")
    if enclosure is not None:
        kind = (enclosure.get("type") or "").lower()
        url = (enclosure.get("url") or "").strip()
        if url and kind.startswith("image/"):
            return url
    media = item.find(f"{MEDIA}content")
    if media is not None:
        url = (media.get("url") or "").strip()
        medium = (media.get("medium") or "").lower()
        kind = (media.get("type") or "").lower()
        if url and (medium == "image" or kind.startswith("image/")):
            return url
    return None


def _parse_date(value: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom), normalized to naive UTC — the same
    convention `later_ink.connectors.base.parse_dt` documents, because ebooklib
    stamps dcterms:modified with a literal Z and no timezone conversion."""
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _text(el) -> str:
    if el is None:
        return ""
    raw = el.text_content() if hasattr(el, "text_content") else el.text or ""
    # Publishers double-encode: the title is HTML-escaped, then XML-escaped, so
    # lxml decodes once and "AI Isn&#x27;t…" survives as literal text. Decoding
    # again is safe here because this is a text field, never markup.
    return " ".join(unescape(raw).split())


def _element_html(el) -> str | None:
    """HTML carried by an RSS/Atom element, whether as text or child nodes."""
    if el is None:
        return None
    if el.text and el.text.strip():
        return el.text
    children = [child for child in el if isinstance(child.tag, str)]
    if children:
        return "".join(etree.tostring(c, encoding="unicode") for c in children)
    return None


def _summary(content: str | None) -> str | None:
    if not content:
        return None
    text = extract.visible_text(content)
    return text[:600] or None


def make_article(feed: Feed, item: FeedItem) -> Article:
    return Article(
        id=item.id,
        title=item.title,
        author=item.author,
        summary=item.summary,
        url=item.link,
        updated=item.published or datetime.now(),
        word_count=extract.word_count(item.content_html) if item.content_html else None,
        category="article",
        image_url=item.image_url,
        content_date=item.published,
    )


async def fetch_feed(url: str, *, timeout: float, max_bytes: int) -> bytes:
    async with httpx.AsyncClient() as client:
        result = await fetch_bytes(client, url, timeout=timeout, max_bytes=max_bytes)
    if result is None:
        raise UpstreamError("Could not fetch that feed", 502)
    body, _media_type = result
    return body


def discover_feed_url(page: bytes, page_url: str) -> str | None:
    """The feed a web page advertises through <link rel="alternate">."""
    try:
        document = lxml_html.fromstring(page)
    except (etree.ParserError, ValueError):
        return None
    for link in document.iter("link"):
        rel = (link.get("rel") or "").lower().split()
        kind = (link.get("type") or "").lower()
        href = link.get("href")
        if href and "alternate" in rel and kind in _FEED_TYPES:
            return urljoin(page_url, href)
    return None


async def resolve_feed(url: str) -> tuple[str, ParsedFeed]:
    """Fetch and parse what the user pasted, which is often a site, not a feed.

    Returns the URL to subscribe to. Raises UpstreamError with a message fit to
    show them when neither the URL nor anything it advertises is a feed.
    """
    timeout = config.get_feed_fetch_timeout()
    max_bytes = config.get_feed_max_bytes()
    body = await fetch_feed(url, timeout=timeout, max_bytes=max_bytes)
    try:
        return url, parse_feed(body, url)
    except UpstreamError as not_a_feed:
        advertised = discover_feed_url(body, url)
        if advertised is None:
            raise UpstreamError("no feed found at that address") from not_a_feed
    body = await fetch_feed(advertised, timeout=timeout, max_bytes=max_bytes)
    return advertised, parse_feed(body, advertised)


def parse_opml(document: bytes) -> list[tuple[str, str]]:
    """(feed URL, title) for every subscription in an OPML file, nested or not."""
    try:
        root = etree.fromstring(document, parser=_PARSER)
    except etree.XMLSyntaxError as e:
        raise UpstreamError("That file is not valid OPML") from e
    if root is None or _localname(root).lower() != "opml":
        raise UpstreamError("That file is not valid OPML")
    found: list[tuple[str, str]] = []
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if url:
            found.append((url, " ".join((outline.get("title") or outline.get("text") or "").split())))
    return found


def to_opml(feeds: list[Feed]) -> str:
    outlines = "\n".join(
        f"    <outline type=\"rss\" text={quoteattr(feed.title or feed.url)}"
        f" xmlUrl={quoteattr(feed.url)}/>"
        for feed in feeds
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n'
        f"  <head><title>Matins feeds</title></head>\n  <body>\n{outlines}\n  </body>\n</opml>\n"
    )


class RssConnector(Connector):
    """One catalogue folder per subscription, ordered as the user added them."""

    name = "rss"
    description = "Feeds"

    def __init__(self, store: MatinsStore, secret: str):
        self._store = store
        self._secret = secret

    async def list_folders(self) -> list[Folder]:
        return [
            Folder(
                id=f.id,
                title=f.title or f.url,
                description=f.url,
            )
            for f in self._store.list_feeds(self._secret)
        ]

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        feed = self._store.get_feed(self._secret, folder_id)
        if feed is None:
            raise UpstreamError("That feed is no longer subscribed", 404)
        parsed = await self._load(feed)
        return [make_article(feed, item) for item in parsed.items], None

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        feed_id, _, _digest = article_id.rpartition("-")
        feed = self._store.get_feed(self._secret, feed_id)
        if feed is None:
            raise ArticleUnavailable("That feed is no longer subscribed", 404)
        parsed = await self._load(feed)
        item = next((i for i in parsed.items if i.id == article_id), None)
        if item is None:
            # Falls out of the window once the feed rolls over, or the publisher
            # edited the link. Neither is a server error.
            raise ArticleUnavailable(
                "That article has dropped out of the feed; open it at the source", 404
            )
        article = make_article(feed, item)

        # Prefer the feed's own full text. Publishers who ship content:encoded
        # have already made the editorial choice; re-extracting from their page
        # would only add a request and a way to get less.
        if item.content_html and extract.word_count(item.content_html) >= 150:
            return article, item.content_html

        page = await self._fetch_page(item.link, cookie=feed.auth_cookie)
        # A wall (paywall interstitial, consent shell, JS challenge) is not an
        # article: keep the feed's own lede rather than printing the error.
        if page and not extract.looks_blocked(page):
            extracted = extract.html_to_content(page, item.link)
            if extracted:
                return article, extracted
        if item.content_html:
            return article, item.content_html
        raise ArticleUnavailable(
            "Could not extract that article; open it at the source", 422
        )

    async def refresh(self, *, force: bool = False) -> None:
        """Load every subscription concurrently; failures land in the store.

        `force` ignores the cache, for "check my feeds now". Otherwise this just
        warms it, so whatever walks the feeds one by one next does not wait for
        them one by one."""
        limit = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def load(feed: Feed) -> None:
            async with limit:
                if force:
                    _cache.pop(f"{feed.id}|{feed.url}", None)
                    _failures.pop(f"{feed.id}|{feed.url}", None)
                try:
                    await self._load(feed)
                except UpstreamError:
                    pass  # recorded by _load

        await asyncio.gather(*(load(feed) for feed in self._store.list_feeds(self._secret)))

    async def _load(self, feed: Feed) -> ParsedFeed:
        # Keyed by feed, not by URL. Item ids embed the feed id, which is
        # derived from the owning secret, so a URL-only cache would hand one
        # catalogue a parsed feed whose ids belong to another — and every
        # download would then fail with "that feed is no longer subscribed".
        # Two users subscribing to the same site is the normal case, not an
        # edge case.
        key = f"{feed.id}|{feed.url}"
        now = time.monotonic()
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            _cache.move_to_end(key)
            return cached[1]
        failed = _failures.get(key)
        if failed and now - failed[0] < _FAILURE_TTL:
            raise UpstreamError(failed[1])
        try:
            body = await fetch_feed(
                feed.url,
                timeout=config.get_feed_fetch_timeout(),
                max_bytes=config.get_feed_max_bytes(),
            )
            parsed = parse_feed(body, feed.url, feed.id)
        except UpstreamError as e:
            self._store.record_feed_fetch(self._secret, feed.id, error=str(e))
            if len(_failures) >= _CACHE_MAX:
                _failures.clear()
            _failures[key] = (now, str(e))
            raise
        _failures.pop(key, None)
        dates = [item.published for item in parsed.items if item.published]
        self._store.record_feed_fetch(
            self._secret,
            feed.id,
            item_count=len(parsed.items),
            last_item_at=max(dates).replace(tzinfo=UTC).timestamp() if dates else None,
        )
        _cache[key] = (now, parsed)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
        # Adopt the feed's own title once, so the catalogue reads like the site
        # rather than like a URL. A rename is kept: an explicit user title wins.
        if not feed.title and parsed.title:
            self._store.rename_feed(self._secret, feed.id, parsed.title[:120])
        return parsed

    async def lede_for(self, url: str) -> str | None:
        """The summary a subscribed feed carries for this article link."""
        for feed in self._store.list_feeds(self._secret):
            try:
                parsed = await self._load(feed)
            except UpstreamError:
                continue
            for item in parsed.items:
                if item.link == url:
                    return item.summary or item.content_html or None
        return None

    async def _fetch_page(self, url: str, *, cookie: str | None = None) -> str | None:
        async with httpx.AsyncClient() as client:
            result = await fetch_bytes(
                client,
                url,
                timeout=config.get_feed_fetch_timeout(),
                max_bytes=config.get_article_max_bytes(),
                headers={"Cookie": cookie} if cookie else None,
            )
        if result is None:
            return None
        body, _media_type = result
        return body.decode("utf-8", errors="replace")


def _recent(items: list[FeedItem], since: datetime | None) -> list[FeedItem]:
    if since is None:
        return items
    return [i for i in items if i.published is None or i.published >= since]


def recent_since(items: list[FeedItem], hours: int) -> list[FeedItem]:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
    return _recent(items, cutoff)


def clear_cache() -> None:
    """Tests only."""
    _cache.clear()
    _failures.clear()

