"""The built-in read-later queue.

later.ink assumes a third party already holds the queue. That is a reasonable
trade for Readwise users, and a hard blocker for everyone else — so Matins ships
its own: a table of links, filled from the settings page, a share shortcut, or
`POST /{secret}/save`. No account, no external service, and it feeds the issue
builder exactly like a feed does.
"""

import re
from datetime import UTC, datetime
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

    def __init__(self, store: MatinsStore, secret: str):
        self._store = store
        self._secret = secret

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
        # Newest first: a saved link is most interesting right after saving it.
        items = list(reversed(self._store.list_saved(self._secret)))
        return [_article(item) for item in items], None

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        try:
            saved_id = int(article_id)
        except ValueError as e:
            raise ArticleUnavailable("Unknown saved item", 404) from e
        item = self._store.get_saved(self._secret, saved_id)
        if item is None:
            raise ArticleUnavailable("That saved item is gone", 404)
        if item.content:
            # Supplied by the caller (API/agent): trust it as text, but it still
            # goes through the same sanitising EPUB path as fetched HTML.
            return _article(item), item.content
        page = await _fetch_page(item.url)
        content = extract.html_to_content(page, item.url) if page else None
        if not content:
            raise ArticleUnavailable(
                "Could not extract that page; open it at the source", 422
            )
        return _article(item), content


def _article(item: SavedItem) -> Article:
    return Article(
        id=str(item.id),
        title=item.title or title_from_url(item.url),
        summary=item.url,
        url=item.url,
        updated=datetime.fromtimestamp(item.added_at, tz=UTC).replace(tzinfo=None),
        content_date=datetime.fromtimestamp(item.added_at, tz=UTC).replace(tzinfo=None),
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
