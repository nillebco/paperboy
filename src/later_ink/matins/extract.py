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

from lxml.html import HtmlElement, document_fromstring, tostring

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
    "verify you are human",
    "unusual traffic",
    "captcha",
)


def looks_blocked(raw: str) -> bool:
    """True when a fetched page is a wall, not an article."""
    text = visible_text(raw).casefold()
    return any(marker in text for marker in _BLOCK_MARKERS)


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
