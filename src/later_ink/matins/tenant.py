"""One catalogue per user, assembled from that user's sources.

Upstream's multi-tenant mode maps a secret to exactly one connector: a Readwise
token. Matins needs the opposite — a user has feeds, a saved queue, and maybe a
Readwise account at the same time — so this class fans out over sub-connectors.

Ids are namespaced so a download routes back to the connector that produced the
article, but only the *new* sources get a prefix. Readwise keeps upstream's bare
ids on purpose: every OPDS URL a reader has already bookmarked, and every
progress entry keyed by article id, keeps working. Ids without a known prefix
resolve to Readwise, which is also what an old client will send.
"""

from .. import config as app_config
from ..connectors.base import Article, Connector, Folder
from ..connectors.readwise import ReadwiseConnector
from .db import MatinsStore
from .queue import QueueConnector
from .rss import RssConnector

LABELS = {
    "readwise": "Readwise",
    "wallabag": "Wallabag",
    "instapaper": "Instapaper",
    "later": "Saved",
    "rss": "Feeds",
}

# Empty prefix = bare ids (upstream compatibility).
PREFIXES = {
    "readwise": "",
    "later": "saved:",
    "rss": "feeds:",
}

# Order is the order they appear in the catalogue. A queue the user built by
# hand is the most intentional thing they have, so it leads; feeds are the
# bulk; a third-party account is another inbox of the same shape.
SOURCE_ORDER = ("later", "rss", "readwise", "wallabag", "instapaper")

_SEARCH_CAP = 50


class TenantReader(Connector):
    name = "matins"
    description = "Your reading"

    def __init__(self, sources: dict[str, Connector]):
        self.sources = sources

    def _ordered(self):
        for name in SOURCE_ORDER:
            if name in self.sources:
                yield name, self.sources[name]

    async def list_folders(self) -> list[Folder]:
        folders: list[Folder] = []
        for name, connector in self._ordered():
            for folder in await connector.list_folders():
                folders.append(
                    Folder(
                        id=self._decorate(name, folder.id),
                        title=self._label(name, folder.title),
                        description=folder.description,
                    )
                )
        return folders

    async def list_views(self) -> list[Folder]:
        views: list[Folder] = []
        for name, connector in self._ordered():
            for view in await connector.list_views():
                views.append(
                    Folder(
                        id=self._decorate(name, view.id),
                        title=self._label(name, view.title),
                        description=view.description,
                    )
                )
        return views

    async def list_articles(
        self, folder_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        name, connector, inner = self._split(folder_id)
        articles, next_cursor = await connector.list_articles(inner, cursor)
        return self._decorate_all(name, articles), next_cursor

    async def list_view_articles(
        self, view_id: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        name, connector, inner = self._split(view_id)
        articles, next_cursor = await connector.list_view_articles(inner, cursor)
        return self._decorate_all(name, articles), next_cursor

    async def get_article_html(self, article_id: str) -> tuple[Article, str]:
        name, connector, inner = self._split(article_id)
        article, html_content = await connector.get_article_html(inner)
        article.id = self._decorate(name, article.id)
        return article, html_content

    async def search(
        self, query: str, cursor: str | None = None
    ) -> tuple[list[Article], str | None]:
        """Search every source, then merge.

        No paging across sources: each connector returns one bounded page and
        the union is capped. A cursor meaning "source 2, page 3" would need the
        same encoding gymnastics as the view scanner, for a result list few
        e-readers page through anyway."""
        merged: list[Article] = []
        seen: set[str] = set()
        for name, connector in self._ordered():
            articles, _ = await connector.search(query)
            for article in articles:
                if article.id in seen:
                    continue
                seen.add(article.id)
                article.id = self._decorate(name, article.id)
                merged.append(article)
        return merged[:_SEARCH_CAP], None

    async def close(self) -> None:
        for connector in self.sources.values():
            await connector.close()

    def decorate_id(self, name: str, inner_id: str) -> str:
        """Public so the issue builder can name an id exactly as this catalogue
        does — a dedupe key that disagrees with the download URL would reprint
        the same article every day."""
        return self._decorate(name, inner_id)

    def _decorate(self, name: str, inner_id: str) -> str:
        return f"{PREFIXES.get(name, f'{name}:')}{inner_id}"

    def _decorate_all(self, name: str, articles: list[Article]) -> list[Article]:
        for article in articles:
            article.id = self._decorate(name, article.id)
        return articles

    def _label(self, name: str, title: str) -> str:
        # An unprefixed source is the default one; labelling it would put a
        # vendor name on folders that upstream users know by their own names.
        if not PREFIXES.get(name, f"{name}:"):
            return title
        return f"{LABELS.get(name, name)} · {title}"

    def _split(self, value: str) -> tuple[str, Connector, str]:
        for name, prefix in PREFIXES.items():
            if prefix and name in self.sources and value.startswith(prefix):
                return name, self.sources[name], value[len(prefix) :]
        if "readwise" in self.sources:
            return "readwise", self.sources["readwise"], value
        # A catalogue with no Readwise account: unprefixed ids can only belong
        # to the single other source, so route them there rather than 404.
        name, connector = next(self._ordered())
        return name, connector, value


def build_tenant_reader(
    secret: str, readwise_token: str | None, store: MatinsStore
) -> TenantReader:
    """Sources for one user. Readwise appears only when a token was stored."""
    feeds = RssConnector(store, secret)
    sources: dict[str, Connector] = {
        "later": QueueConnector(store, secret, find_lede=feeds.lede_for),
        "rss": feeds,
    }
    if readwise_token:
        sources["readwise"] = ReadwiseConnector(
            readwise_token, app_config.get_readwise_categories()
        )
    return TenantReader(sources)
