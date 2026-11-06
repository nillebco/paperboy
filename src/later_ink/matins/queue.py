"""The built-in read-later queue.

later.ink assumes a third party already holds the queue. That is a reasonable
trade for Readwise users, and a hard blocker for everyone else — so Matins ships
its own: a table of links, filled from the settings page, a share shortcut, or
`POST /{secret}/save`. No account, no external service, and it feeds the issue
builder exactly like a feed does.
"""

import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape
from urllib.parse import unquote, urlsplit

import httpx

from ..connectors.base import (
    Article,
    ArticleUnavailable,
    Connector,
    Folder,
    UpstreamError,
)
from ..fetch import fetch_bytes
from . import config, extract
from .db import MatinsStore, SavedItem

INBOX = "inbox"


class QueueConnector(Connector):
    name = "later"
    description = "Saved for later"

    def __init__(
        self,
        store: MatinsStore,
        secret: str,
        find_lede: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        self._store = store
        self._secret = secret
        self._find_lede = find_lede

    async def list_folders(self) -> list[Folder]:
        pending = len(self._store.list_saved(self._secret))
        return [
            Folder(
                id=INBOX,
                title="Read later",
                description=f"{pending} saved" if pending else "Nothing saved yet",
            )
        ]

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        if folder_id != INBOX:
            raise UpstreamError("Unknown folder", 404)
        # What a person picked leads, oldest first, so a pick can never be pushed
        # out by newer ones. The curator's picks follow, newest first: it queues
        # its worst pick first, so the best one prints first.
        saved = self._store.list_saved(self._secret)
        picked = [item for item in saved if item.origin != "curator"]
        curated = [item for item in reversed(saved) if item.origin == "curator"]
        return [_article(item) for item in picked + curated], None

    async def lede_for(self, url: str) -> str | None:
        """The summary a subscribed feed carries for this link, if any."""
        return await self._find_lede(url) if self._find_lede else None

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        try:
            saved_id = int(article_id)
        except ValueError as e:
            raise ArticleUnavailable("Unknown saved item", 404) from e
        item = self._store.get_saved(self._secret, saved_id)
        if item is None:
            raise ArticleUnavailable("That saved item is gone", 404)
        if item.content:
            # A post that only points at an article prints the article.
            content = await linked_content(item.content)
            if content:
                return _article(item), _with_recap(item.recap, content)
            # Supplied by the caller (API/agent): trust it as text, but it still
            # goes through the same sanitising EPUB path as fetched HTML.
            return _article(item), _with_recap(item.recap, item.content)
        page = await _fetch_page(item.url)
        # A wall (paywall, consent shell, JS challenge) is not an article.
        blocked = page is not None and extract.looks_blocked(page)
        content = extract.html_to_content(page, item.url) if page and not blocked else None
        if not content:
            lede = item.lede or await self.lede_for(item.url)
            if not lede:
                raise ArticleUnavailable(
                    "Could not extract that page; open it at the source", 422
                )
            content = f"<p>{escape(extract.visible_text(lede))}</p>"
        return _article(item), _with_recap(item.recap, content)


def _with_recap(recap: str | None, html: str) -> str:
    if not recap:
        return html
    paragraphs = "".join(
        f"<p>{escape(line.strip())}</p>" for line in recap.splitlines() if line.strip()
    )
    return f"<blockquote><p><strong>Recap</strong></p>{paragraphs}</blockquote><hr/>{html}"


def _article(item: SavedItem) -> Article:
    return Article(
        id=str(item.id),
        title=item.title or title_from_url(item.url),
        summary=item.url,
        url=item.url,
        updated=datetime.fromtimestamp(item.added_at, tz=UTC).replace(tzinfo=None),
        content_date=datetime.fromtimestamp(item.added_at, tz=UTC).replace(tzinfo=None),
        published=(
            datetime.fromtimestamp(item.published, tz=UTC).replace(tzinfo=None)
            if item.published
            else None
        ),
        category="saved",
    )


def title_from_url(url: str) -> str:
    """A readable placeholder so the catalogue does not show only links.

    Kept dumb on purpose: the real title arrives when the EPUB is built (the
    page's <title> is right there in the extracted document), and a listing that
    fetched every saved page would turn opening a folder into N requests.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return url
    last = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    last = re.sub(r"\.(html?|php|aspx?|md)$", "", last, flags=re.IGNORECASE)
    words = re.sub(r"[-_+]+", " ", last).strip()
    if not words:
        return urlsplit(url).hostname or url
    return words[:1].upper() + words[1:]


# The reader opens the same post again and again (list, issue view, back): keep
# what the link gave for a while instead of fetching the publisher every time.
_LINKED_TTL = 6 * 3600
_LINKED_MAX = 64
_linked: dict[str, tuple[float, str | None]] = {}
_WAYBACK = "https://web.archive.org/web/2id_/"


async def linked_content(html: str) -> str | None:
    """The linked article's body when `html` is only a line and a link, else None.

    None too when the link is a wall or extracts to nothing: the caller keeps
    the post."""
    link = extract.linked_article(html) or extract.read_more_link(html)
    if not link:
        return None
    now = time.monotonic()
    hit = _linked.get(link)
    # A failure is retried sooner: the publisher may only have been slow.
    if hit and now - hit[0] < (_LINKED_TTL if hit[1] else 600):
        return hit[1]
    teaser = extract.word_count(html)
    content = None
    # Some publishers refuse servers outright (openai.com answers 403 behind
    # Cloudflare). The Wayback Machine's raw copy (id_: no toolbar, original
    # markup) is then the article as published.
    for url in (link, f"{_WAYBACK}{link}"):
        page = await _fetch_page(url)
        if page and not extract.looks_blocked(page):
            content = extract.html_to_content(page, link)
            # Only an article clearly longer than the teaser replaces it: an
            # extraction that found a cookie notice must not.
            if content and extract.word_count(content) < 1.5 * teaser:
                content = None
            if content:
                break
    if len(_linked) >= _LINKED_MAX:
        _linked.pop(next(iter(_linked)))
    _linked[link] = (now, content)
    return content


async def _fetch_page(url: str) -> str | None:
    async with httpx.AsyncClient() as client:
        result = await fetch_bytes(
            client,
            url,
            timeout=config.get_feed_fetch_timeout(),
            max_bytes=config.get_article_max_bytes(),
        )
    if result is None:
        return None
    body, _media_type = result
    return body.decode("utf-8", errors="replace")
