"""Rendering an article for the browser.

The OPDS path hands HTML to the EPUB builder, which strips active content and
embeds images. The reader needs a different transformation for the same input:
no embedding (images stay remote — they are already on the reader's own
network), links must not navigate the app away, and the whole page has to be
safe to put inside our own shell.

Sanitising reuses `later_ink.epub._strip_active_content` rather than growing a
second implementation: it is the code path that already removes event handlers
and `javascript:`/`data:` URLs from untrusted article HTML, and it is exercised
by the EPUB tests.
"""

import logging
from html import escape

from lxml import etree
from lxml.html import fromstring

from ..epub import _strip_active_content

logger = logging.getLogger(__name__)


def to_page_html(raw: str) -> str | None:
    """Article HTML, sanitised, with outbound links opening in a new tab."""
    if not raw or not raw.strip():
        return None
    try:
        doc = fromstring(raw)
        for el in doc.xpath("//script | //style | //form | //input | //button"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
        _strip_active_content(doc)
        for link in doc.iter("a"):
            href = (link.get("href") or "").strip()
            if href.startswith(("http://", "https://")):
                link.set("target", "_blank")
                link.set("rel", "noopener noreferrer")
        # A fragment like "<p>text</p>" parses to the <p> itself, not a wrapper,
        # so iterating children would yield nothing and render a blank article.
        parts = [etree.tostring(child, encoding="unicode", method="html") for child in doc]
        if not parts:
            return etree.tostring(doc, encoding="unicode", method="html")
        if doc.text and doc.text.strip():
            parts.insert(0, escape(doc.text.strip()))
        return "".join(parts)
    except Exception:
        logger.warning("could not sanitise article HTML", exc_info=True)
        return None
