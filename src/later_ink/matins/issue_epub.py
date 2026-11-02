"""Compose one EPUB from many articles: a dated issue.

This is the piece later.ink does not have. Upstream converts one item to one
EPUB on demand; an issue is the opposite object — a fixed set of items, chosen
once, ordered deliberately, and identical for every device that downloads it.
That is also why it is written to disk rather than rendered per request: an
issue is an edition, not a query.

Image embedding, active-content stripping and ZIP timestamp pinning are reused
from `later_ink.epub` rather than reimplemented, so an issue inherits the same
hardened HTML path as a single article.
"""

import io
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

import httpx
from ebooklib import epub
from lxml.html import fromstring

from .. import covers
from ..epub import (
    NAV_CSS,
    NORMALIZE_CSS,
    UNKNOWN_DATE,
    BuildResult,
    _embed_images,
    _pin_zip_timestamps,
    _serialize,
    _strip_active_content,
)

# An issue is one download, so it gets a larger image budget than a single
# article — but still a bounded one, because the whole point is that it arrives
# on a slow e-ink radio in the morning.
MAX_ISSUE_IMAGES = 90
MAX_ISSUE_IMAGE_BYTES = 30 * 1024 * 1024
# One wall-clock budget for the whole issue rather than per article: fifteen
# articles each waiting out their own image phase is a download measured in
# hours, and the reader is waiting for it.
ISSUE_IMAGE_PHASE_BUDGET = 180.0


@dataclass
class IssueItem:
    title: str
    html: str
    section: str
    author: str | None = None
    source_url: str | None = None
    image_url: str | None = None
    word_count: int = 0
    # Shown instead of the issue date; empty when the source gives no date.
    published: str = ""
    show_dates: bool = False


@dataclass
class IssueBuild:
    result: BuildResult
    sections: list[str] = field(default_factory=list)


async def build_issue(
    *,
    title: str,
    issue_date: datetime,
    items: list[IssueItem],
    language: str = "en",
    image_client: httpx.AsyncClient | None = None,
) -> IssueBuild:
    if not items:
        raise ValueError("an issue needs at least one item")

    book = epub.EpubBook()
    book.set_identifier(f"matins-issue-{issue_date:%Y%m%d}-{len(items)}")
    book.set_title(title)
    book.set_language(language)
    book.add_metadata("DC", "publisher", "Matins")
    book.add_metadata("DC", "date", issue_date.strftime("%Y-%m-%d"))
    for item in items:
        if item.source_url:
            book.add_metadata("DC", "source", item.source_url)

    owns_client = image_client is None
    client = image_client or httpx.AsyncClient()
    chapters: list[epub.EpubHtml] = []
    image_items: list[epub.EpubItem] = []
    images_failed = False
    budget_exhausted = False
    fallback_used = False
    image_deadline = time.monotonic() + ISSUE_IMAGE_PHASE_BUDGET
    image_budget = MAX_ISSUE_IMAGE_BYTES

    try:
        for index, item in enumerate(items):
            try:
                doc = fromstring(item.html)
                for el in doc.xpath("//script | //style"):
                    parent = el.getparent()
                    if parent is not None:
                        parent.remove(el)
                _strip_active_content(doc)
                # Prefix per chapter: every document numbers its images from
                # zero, so one book with fifteen articles would otherwise keep
                # fifteen copies of `images/img0.png` and show the last one in
                # all fifteen places.
                embedded, failed, exhausted = await _embed_images(
                    doc,
                    client,
                    prefix=f"c{index:03d}-",
                    max_images=max(0, MAX_ISSUE_IMAGES - len(image_items)),
                    max_total_bytes=image_budget,
                    deadline=image_deadline,
                )
                image_budget -= sum(len(item.content) for item in embedded)
                image_items.extend(embedded)
                images_failed = images_failed or failed
                budget_exhausted = budget_exhausted or exhausted
                body = _serialize(doc)
            except Exception:
                # One unparseable article must not cost the rest of the paper.
                fallback_used = True
                body = _fallback(item)

            chapter = epub.EpubHtml(
                uid=f"item_{index:03d}",
                title=item.title or f"Item {index + 1}",
                file_name=f"item_{index:03d}.xhtml",
                lang=language,
            )
            xhtml = (
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                f"<title>{escape(item.title or '')}</title></head><body>"
                f"{_masthead(item, issue_date)}"
                f"<h1>{escape(item.title or 'Untitled')}</h1>"
                f"{body}"
                f"{_source_line(item)}"
                "</body></html>"
            )
            chapter.set_content(xhtml.encode("utf-8"))
            chapter.add_link(href="style/main.css", rel="stylesheet", type="text/css")
            book.add_item(chapter)
            chapters.append(chapter)
    finally:
        if owns_client:
            await client.aclose()

    book.set_cover("cover.jpg", covers.make_cover(None, title, None), create_page=True)

    book.add_item(
        epub.EpubItem(
            uid="style",
            file_name="style/main.css",
            media_type="text/css",
            content=(NORMALIZE_CSS + _ISSUE_CSS).encode("utf-8"),
        )
    )
    for item in image_items:
        book.add_item(item)

    book.toc = _toc(items, chapters)
    book.add_item(epub.EpubNcx())
    nav = epub.EpubNav()
    nav_css = epub.EpubItem(
        uid="nav_style",
        file_name="style/nav.css",
        media_type="text/css",
        content=NAV_CSS.encode("utf-8"),
    )
    book.add_item(nav_css)
    nav.add_link(href="style/nav.css", rel="stylesheet", type="text/css")
    book.add_item(nav)
    book.spine = ["cover", "nav", *chapters]

    # An issue is a new edition every day, so the ZIP timestamps are pinned to
    # the issue date rather than a constant: two downloads of the same issue are
    # byte-identical (what KOReader's progress sync needs), while tomorrow's
    # issue is properly dated.
    mtime = issue_date.replace(tzinfo=None) if issue_date.tzinfo else issue_date
    buf = io.BytesIO()
    epub.write_epub(buf, book, {"mtime": mtime if mtime else UNKNOWN_DATE})
    return IssueBuild(
        result=BuildResult(
            data=_pin_zip_timestamps(buf.getvalue()),
            fallback_used=fallback_used,
            images_failed=images_failed,
            budget_exhausted=budget_exhausted,
        ),
        sections=sorted({item.section for item in items}),
    )


_ISSUE_CSS = (
    " .matins-meta { font-size: 0.8em; letter-spacing: 0.04em; text-transform: uppercase;"
    " color: #666; margin-bottom: 0.4em; }"
    " .matins-source { font-size: 0.85em; margin-top: 2em; border-top: 1px solid #ddd;"
    " padding-top: 0.6em; }"
    " .matins-section { font-size: 1.05em; margin-top: 0; }"
)


def _masthead(item: IssueItem, issue_date: datetime) -> str:
    if item.show_dates:
        parts = [part for part in (item.section, item.published) if part]
        return f'<p class="matins-meta">{escape(" — ".join(parts))}</p>' if parts else ""
    section = f"{escape(item.section)} — " if item.section else ""
    return f'<p class="matins-meta">{section}{issue_date:%A %d %B %Y}</p>'


def _source_line(item: IssueItem) -> str:
    if not item.source_url:
        return ""
    author = f"{escape(item.author)} · " if item.author else ""
    return (
        f'<p class="matins-source">{author}<a href="{escape(item.source_url)}">'
        f"Read at the source</a></p>"
    )


def _fallback(item: IssueItem) -> str:
    link = (
        f'<p><a href="{escape(item.source_url)}">{escape(item.source_url)}</a></p>'
        if item.source_url
        else ""
    )
    return f"<p>This item could not be converted for offline reading.</p>{link}"


def _toc(items: list[IssueItem], chapters: list[epub.EpubHtml]) -> list:
    """Group the table of contents by section, preserving article order.

    ebooklib accepts (Section, [chapters]) tuples, which is what makes the
    ToC read like a newspaper's sections instead of a flat list of headlines.
    """
    grouped: dict[str, list[epub.EpubHtml]] = {}
    for item, chapter in zip(items, chapters, strict=True):
        grouped.setdefault(item.section, []).append(chapter)
    if len(grouped) == 1:
        return chapters
    return [(epub.Section(section), members) for section, members in grouped.items()]
