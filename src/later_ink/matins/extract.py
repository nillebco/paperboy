"""Turn a web page into readable article HTML, with no new dependency.

The upstream connectors never need this: Readwise, Wallabag and Instapaper have
already extracted the article before we see it. RSS does not — a feed hands us a
summary, sometimes full content, sometimes only a headline — so Matins has to do
its own extraction.

Deliberately hand-rolled over lxml (already a pinned dependency) rather than
pulling in a readability port. A new runtime dependency is a new supply-chain
surface to audit, pin, hash and patch, for something that a hundred lines of
heuristics covers well enough for a morning paper. Where it fails, the item
falls back to the feed's own content, which is what the feed author chose to
publish anyway.
"""

import re
from html import escape
from urllib.parse import urlsplit

from lxml.html import HtmlElement, document_fromstring, tostring
from readability import Document

# Removed before scoring, not after: a page whose navigation is a third of its
# text should not out-score the article for that reason.
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "object",
    "embed",
    "form",
    "button",
    "nav",
    "aside",
    "footer",
    "header",
    "menu",
    "dialog",
)
_DROP_ROLES = {"navigation", "complementary", "banner", "contentinfo", "search", "dialog"}

_CONTAINER_TAGS = {"article", "main"}

# Class/id hints, the same words every readability implementation ends up with.
_HINT = re.compile(
    r"(?:^|[\s_-])(article|post|entry|content|story|body-text|markdown|prose)(?:[\s_-]|$)",
    re.IGNORECASE,
)

# Below this, the winner is a teaser card rather than the article, and the
# surrounding <body> is the better bet.
_MIN_CANDIDATE_CHARS = 400
# Below this, there is no article here — a paywall interstitial, a login page,
# or a JS-only shell. The caller then keeps the feed's summary.
_MIN_ARTICLE_CHARS = 200
_MAX_WALL_WORDS = 400

# A forum page is a short post above an endless thread: print the post and the
# first few replies. Only a short body counts: a long article with comments below
# is an article, and readability already leaves its comments out.
_COMMENT_CLASS = re.compile(r"^(comment|commtext|comment[-_](body|content|text))$", re.I)
_MIN_COMMENTS = 5
_MAX_COMMENTS = 8
_MIN_COMMENT_WORDS = 15
_MAX_DISCUSSION_BODY_WORDS = 300
# Readability sometimes keeps one paragraph of a page: below this share of what
# the wider heuristic found, trust the heuristic.
_MIN_READABLE_SHARE = 0.2

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Pages that answer 200 with a wall instead of the article. Le Monde's is the
# one that prompted this list ("A required part of this site couldn't load");
# the extractor would otherwise print that error as the article body.
_BLOCK_MARKERS = (
    "a required part of this site couldn't load",
    "une partie requise de ce site n'a pas pu se charger",
    "please enable javascript",
    "activez javascript",
    "javascript is disabled",
    "access denied",
    "just a moment...",
    "enable javascript and cookies",
    "verify you are human",
    "unusual traffic",
)


def looks_blocked(raw: str) -> bool:
    """True when a fetched page is a wall, not an article."""
    text = visible_text(raw).casefold().replace("\u2019", "'")
    # A wall is short. A long article may well quote "access denied".
    if len(text.split()) > _MAX_WALL_WORDS:
        return False
    return any(marker in text for marker in _BLOCK_MARKERS)


# A post that is a headline and a link ("Title | Discussion https://…") has
# nothing to print of its own: the article is behind the link. Above this many
# words of its own, the post is commentary worth keeping.
_MAX_LINK_POST_WORDS = 30
_URL = re.compile(r"https?://\S+")
# Links that are never the article: the post itself, discussion threads, images.
_NOT_ARTICLE_HOSTS = ("bsky.app", "cdn.bsky.app", "news.ycombinator.com")
_IMAGE_PATH = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg)$", re.IGNORECASE)


def linked_article(html: str) -> str | None:
    """The article URL when an HTML fragment is only a short line and a link.

    Supplied content from a Bluesky bot is "Title | Discussion <url>": printing
    it would give the reader a link on paper. None when the fragment says
    enough on its own, or links nowhere worth fetching."""
    try:
        doc = document_fromstring(html)
    except (ValueError, TypeError):
        return None
    hrefs = [a.get("href") or "" for a in doc.iter("a")]
    text = doc.text_content()
    hrefs += _URL.findall(text)
    own_words = [w for w in _URL.sub(" ", text).split() if any(c.isalnum() for c in w)]
    if len(own_words) > _MAX_LINK_POST_WORDS:
        return None
    for href in hrefs:
        parts = urlsplit(href.rstrip(".,;)"))
        host = (parts.hostname or "").removeprefix("www.")
        if parts.scheme not in ("http", "https") or not host:
            continue
        if host in _NOT_ARTICLE_HOSTS or _IMAGE_PATH.search(parts.path):
            continue
        return href.rstrip(".,;)")
    return None


# "Read the full article on <a>Le Monde</a>", "Lire la suite", "Leggi tutto":
# a teaser that says where the article is. Not "Read also" / "Leggi anche",
# which point at another article.
_READ_MORE = re.compile(
    r"\b(?:read|see|view)\s+(?:the\s+)?(?:full|whole|complete|entire|rest|original)\b"
    r"|\bread\s+more\b|\bcontinue\s+reading\b|\bfull\s+(?:article|story)\b"
    r"|\blire\s+(?:la\s+suite|l['’]article)\b|\bcontinuer\s+(?:à|a)\s+lire\b"
    r"|\barticle\s+complet\b|\bleggi\s+(?:tutto|l['’]articolo)\b"
    r"|\bcontinua\s+a\s+leggere\b|\bseguir\s+leyendo\b|\bleer\s+m[aá]s\b"
    r"|\bweiterlesen\b|\bleia\s+mais\b",
    re.IGNORECASE,
)
# The line itself is short; a long paragraph that happens to say "read more"
# is prose, not a pointer.
_MAX_READ_MORE_WORDS = 15


def read_more_link(html: str) -> str | None:
    """The URL a teaser's "read the full article" line points at, or None."""
    try:
        doc = document_fromstring(html)
    except (ValueError, TypeError):
        return None
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https") or _IMAGE_PATH.search(parts.path):
            continue
        host = (parts.hostname or "").removeprefix("www.")
        if host in _NOT_ARTICLE_HOSTS:
            continue
        line = a.getparent() if a.getparent() is not None else a
        while line.tag in ("a", "strong", "em", "b", "i", "span") and line.getparent() is not None:
            line = line.getparent()
        text = " ".join(line.text_content().split())
        if len(text.split()) <= _MAX_READ_MORE_WORDS and _READ_MORE.search(text):
            return href
        if _READ_MORE.search(a.text_content()):
            return href
    return None


def visible_text(html: str) -> str:
    """Plain text of an HTML fragment, for word counts and length checks."""
    try:
        doc = document_fromstring(html)
    except (ValueError, TypeError):
        return ""
    text = doc.text_content()
    return _BLANK_LINES.sub("\n\n", _WS.sub(" ", text)).strip()


def word_count(html: str) -> int:
    return len(visible_text(html).split())


def html_to_content(raw: str, base_url: str) -> str | None:
    """Best-effort article body as an HTML fragment, or None.

    Absolute links and images are resolved here rather than in the EPUB
    builder, so the same fragment is correct whether it is downloaded now or
    rendered into an issue later.
    """
    if not raw or not raw.strip():
        return None
    try:
        doc = document_fromstring(raw)
    except (ValueError, TypeError):
        return None

    try:
        doc.make_links_absolute(base_url, resolve_base_href=True)
    except (ValueError, TypeError):
        # A malformed <base> should cost us absolute links, not the article.
        pass

    readable = _readable(doc)
    replies = _replies(doc)
    if len(replies) >= _MIN_COMMENTS and word_count(readable or "<p></p>") <= _MAX_DISCUSSION_BODY_WORDS:
        return (readable or "") + _discussion(replies)

    heuristic = _heuristic(doc)
    if readable and len(visible_text(readable)) >= _MIN_ARTICLE_CHARS:
        if not heuristic or word_count(readable) >= _MIN_READABLE_SHARE * word_count(heuristic):
            return readable
    return heuristic


def _readable(doc: HtmlElement) -> str | None:
    """The article as Mozilla's Readability algorithm sees it: no share bars,
    no navigation, and layout tables gone."""
    try:
        return Document(tostring(doc, encoding="unicode")).summary(html_partial=True)
    except Exception:  # noqa: BLE001 - any failure here just means the fallback
        return None


def _replies(doc: HtmlElement) -> list[str]:
    """Comment texts in page order, innermost element only, skipping one-liners."""
    found = []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if not any(_COMMENT_CLASS.match(token) for token in (el.get("class") or "").split()):
            continue
        nested = any(
            _COMMENT_CLASS.match(token)
            for child in el.iterdescendants()
            if isinstance(child.tag, str)
            for token in (child.get("class") or "").split()
        )
        if not nested:
            found.append(_WS.sub(" ", el.text_content()).strip())
    return [text for text in found if len(text.split()) >= _MIN_COMMENT_WORDS]


def _discussion(replies: list[str]) -> str:
    quotes = "".join(
        f"<blockquote><p>{escape(text)}</p></blockquote>" for text in replies[:_MAX_COMMENTS]
    )
    more = len(replies) - _MAX_COMMENTS
    tail = f"<p><em>{more} more replies at the source.</em></p>" if more > 0 else ""
    return f"<h3>From the discussion</h3>{quotes}{tail}"


def _heuristic(doc: HtmlElement) -> str | None:
    """Largest text container. Used when readability finds next to nothing."""
    _drop(doc)

    best: HtmlElement | None = None
    best_score = 0
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag not in _CONTAINER_TAGS and not _hinted(el):
            continue
        score = _score(el)
        if score > best_score:
            best, best_score = el, score

    if best is not None and best_score >= _MIN_CANDIDATE_CHARS:
        fragment = _inner_html(best)
    else:
        body = doc.find(".//body")
        fragment = _inner_html(body if body is not None else doc)

    if len(visible_text(fragment)) < _MIN_ARTICLE_CHARS:
        return None
    return fragment


def _hinted(el: HtmlElement) -> bool:
    tag = el.tag
    if tag not in ("div", "section", "td"):
        return False
    marker = f"{el.get('class') or ''} {el.get('id') or ''}"
    return bool(_HINT.search(marker))


def _score(el: HtmlElement) -> int:
    """Text length, discounted for link-heavy containers.

    A comment list and an article have the same word count; the difference is
    how much of that text is anchor text. The penalty is what makes "content"
    beat "related links"."""
    text_len = len(el.text_content())
    link_len = sum(len(a.text_content()) for a in el.iter("a"))
    return text_len - 2 * link_len


def _drop(doc: HtmlElement) -> None:
    for tag in _DROP_TAGS:
        for el in doc.iter(tag):
            _remove(el)
    # lxml lowercases attributes, so a case-insensitive compare is redundant —
    # but pages do ship role="Navigation", and the parser preserves the value.
    for el in doc.iter():
        role = el.get("role") if isinstance(el.tag, str) else None
        if role and role.strip().lower() in _DROP_ROLES:
            _remove(el)
    # Links to "#comments" and share widgets survive tag-level filtering; the
    # text-length check above keeps a stray one from winning, but they still
    # end up inside a body fallback.
    for el in doc.xpath("//*[@style and contains(translate(@style,' ',''),'display:none')]"):
        _remove(el)


def _remove(el) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _inner_html(el) -> str:
    parts = [tostring(child, encoding="unicode") for child in el]
    if not parts:
        # A container with only text (rare, but legal HTML) would serialize as
        # an empty string and read as "no article".
        text = (el.text or "").strip()
        return f"<p>{text}</p>" if text else ""
    if el.text and el.text.strip():
        parts.insert(0, el.text.strip())
    return "".join(parts)
