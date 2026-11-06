"""Matins HTTP surface: catalogue dashboard, sources, reader, issues, admin.

Mounted before the tenant routes in `later_ink.main`, which is what makes
`/{secret}/issues/` and `/{secret}/read` reach these handlers instead of the
generic folder route.

Auth is the catalogue secret itself, the same bearer-in-the-URL model the rest of
the app uses. Every state-changing form additionally carries a CSRF token derived
from the app key, because a browser is the only thing that submits them.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets as pysecrets
from datetime import UTC, datetime
from html import escape as html_escape
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from lxml.html import fragment_fromstring, tostring

from .. import config as app_config
from .. import opds
from ..connectors.base import Article, ArticleUnavailable, UpstreamError
from . import catalogues, config, extract, notify, pages, rss, voices, webhooks
from .issues import (
    IssueLimits,
    IssueService,
    limits_for,
    local_day,
    next_run,
    published_label,
)
from .queue import linked_content
from .reader import to_page_html

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FEEDS = 100
MAX_URL_LENGTH = 2048
# The list is one page, no paging: a few hundred rows are cheaper to render than
# a "load more" round trip, and the counts must not promise rows the page omits.
MAX_READER_ITEMS = 400
EPUB_MEDIA = "application/epub+zip"
MIN_KNOWN_WORDS = 150
MAX_OPML_BYTES = 1024 * 1024

# The home page shows what the next issue holds, which can mean fetching feeds.
# Past this it renders without the count rather than making the page feel broken.
HOME_PLAN_TIMEOUT = 3.0
FEEDS_REFRESH_TIMEOUT = 45.0
# A compose answers directly if it finishes this fast; otherwise the browser is
# sent to a page that polls, so a slow build never looks like a hung request.
COMPOSE_WAIT = 6.0
SKIP_LIST_MAX_AGE = 14 * 24 * 3600

# Where a form may return to, by name: a closed list cannot be an open redirect.
BACK_TARGETS = {"home": "", "feeds": "feeds", "next": "issues/preview", "read": "read"}

# Accepted range per catalogue, for the issue limits in settings.
LIMIT_BOUNDS = {
    "max_articles": (1, 100),
    "max_per_feed": (1, 50),
    "max_words": (500, 200_000),
    "window_hours": (1, 720),
    "max_saved": (1, 100),
}

# Sign-in codes: six digits, ten minutes, five tries, one send a minute.
OTP_TTL = 600.0
OTP_MAX_ATTEMPTS = 5

# Article pages are the one place that may load remote images: proxying them
# would mean fetching third-party content on the user's behalf and caching it,
# which is the EPUB builder's job, not the browser's. Links still cannot run
# anything, and there is no script of our own on the page.
def _reader_csp() -> str:
    """Reader pages allow exactly our three scripts, by hash: the scroll
    restorer, the background row actions (which may only call back to this
    origin) and the Listen player. The player's synthesis runs in a worker
    served from /assets/tts/, which carries its own policy."""
    hashes = " ".join(
        "'sha256-"
        + base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode()
        + "'"
        for js in (pages.READER_SCROLL_JS, pages.READER_ACTIONS_JS, pages.READER_LISTEN_JS)
    )
    return (
        "default-src 'none'; img-src https: http: data:; "
        "style-src 'unsafe-inline'; "
        f"script-src {hashes}; connect-src 'self'; worker-src 'self'; "
        "base-uri 'none'; form-action 'self'"
    )


READER_CSP = _reader_csp()


# --------------------------------------------------------------- validation


def _valid_url(raw: str) -> str | None:
    value = raw.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return value


# ------------------------------------------------------------ main helpers
# Imported lazily: `later_ink.main` imports this module while it is still
# executing, so a module-level import would be circular.


def _resolve(secret: str, request: Request) -> str:
    from ..main import _resolve_secret

    return _resolve_secret(secret, request)


def _csrf_token(secret: str) -> str:
    from ..main import _csrf_token as token

    return token(secret)


def _require_csrf(secret: str, token: str | None) -> None:
    from ..main import _require_csrf as require

    require(secret, token)


def _origin(request: Request) -> str:
    from ..main import _feed_origin

    return _feed_origin(request)


def _store(request: Request):
    return request.app.state.matins


def _service(request: Request) -> IssueService:
    return request.app.state.issues


# ------------------------------------------------------------------ sign-in


@router.get("/signin", response_class=HTMLResponse)
async def signin_form(error: str | None = None, notice: str | None = None):
    return _html(pages.signin_page(notice=notice, error=error))


@router.post("/signin", response_class=HTMLResponse)
async def signin_request(request: Request, email: str = Form("")):
    """Email a code. Never says whether the address already has a catalogue."""
    address = _normalize_email(email)
    if address is None:
        return _html(pages.signin_page(error="That does not look like an email address."))

    limiter = request.app.state.otp_limiter
    if not limiter.try_record(f"email:{address}"):
        return _html(
            pages.code_page(
                email=address,
                error="Too many codes requested for that address; try again later.",
            ),
            status=429,
        )

    store = _store(request)
    if not store.otp_cooldown_ok(address, cooldown=config.get_otp_cooldown()):
        return _html(
            pages.code_page(email=address, error="A code was just sent — check your inbox.")
        )

    code = f"{pysecrets.randbelow(1_000_000):06d}"
    store.set_otp(address, _code_hash(request, address, code), ttl=OTP_TTL)
    if not await notify.send_code(address, code):
        # Do not leave a live challenge behind that nobody can be told about.
        store.clear_otp(address)
        return _html(
            pages.signin_page(
                error="Could not send the email. Try again in a minute, or use the token form."
            ),
            status=503,
        )
    return _html(pages.code_page(email=address))


@router.post("/signin/code")
async def signin_verify(
    request: Request, email: str = Form(""), code: str = Form("")
):
    address = _normalize_email(email)
    if address is None:
        return _html(pages.signin_page(error="That does not look like an email address."))

    store = _store(request)
    state = store.otp_state(address)
    if state is None or state["expires_at"] < datetime.now(UTC).timestamp():
        return _html(pages.code_page(email=address, error="That code has expired. Ask for another."))
    if state["attempts"] >= OTP_MAX_ATTEMPTS:
        return _html(
            pages.code_page(email=address, error="Too many attempts. Ask for a new code."),
            status=429,
        )
    if not hmac.compare_digest(state["code_hash"], _code_hash(request, address, code.strip())):
        store.bump_otp_attempts(address)
        return _html(pages.code_page(email=address, error="That code is not right."), status=400)

    store.clear_otp(address)
    secret = store.secret_for_email(address)
    if secret is None or request.app.state.store.get_token(secret) is None:
        secret = request.app.state.store.create_user("")
        store.link_email(secret, address)
    _ensure_delivery(request, secret)
    response = RedirectResponse(f"/{secret}/", status_code=303)
    catalogues.remember(secret, request, response)
    return response


def _normalize_email(raw: str) -> str | None:
    address = (raw or "").strip().lower()
    if not address or len(address) > 254 or " " in address:
        return None
    local, sep, domain = address.partition("@")
    if not sep or not local or "." not in domain or domain.startswith("."):
        return None
    return address


def _code_hash(request: Request, email: str, code: str) -> str:
    key = request.app.state.csrf_key
    return hmac.new(key, f"otp:{email}:{code}".encode(), "sha256").hexdigest()


# ------------------------------------------------------------------ sources


def subscribe(request: Request, secret: str, url: str, parsed):
    """Add a checked subscription; the first one also turns on the daily issue."""
    store = _store(request)
    feed = store.add_feed(secret, url, parsed.title[:120])
    dates = [item.published for item in parsed.items if item.published]
    store.record_feed_fetch(
        secret,
        feed.id,
        item_count=len(parsed.items),
        last_item_at=max(dates).replace(tzinfo=UTC).timestamp() if dates else None,
    )
    _ensure_delivery(request, secret)
    return feed


def _ensure_delivery(request: Request, secret: str) -> None:
    store = _store(request)
    store.ensure_delivery(
        secret,
        schedule=config.get_default_schedule(),
        timezone=config.get_default_timezone(),
        email=store.email_for_secret(secret),
    )


@router.get("/{secret}/feeds", response_class=HTMLResponse)
async def feeds_index(
    secret: str, request: Request, notice: str | None = None, error: str | None = None
):
    """Rendered from what the last fetch recorded: no network, so it is instant."""
    _resolve(secret, request)
    store = _store(request)
    return _html(
        pages.feeds_page(
            secret=secret,
            csrf=_csrf_token(secret),
            feeds=_feed_rows(request, secret),
            bluesky=store.candidate_stats(secret),
            notice=notice,
            error=error,
        )
    )


@router.post("/{secret}/feeds")
async def add_feed(
    secret: str, request: Request, csrf: str = Form(""), url: str = Form("")
):
    """Add one subscription, validating it now rather than at 07:00 tomorrow.

    The URL is fetched and parsed once here so a typo, a 404 or an HTML page
    pasted as a feed produces an immediate, specific message instead of a
    silently empty issue. A page that advertises a feed subscribes to that feed.
    """
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    target = _valid_url(url)
    if not target:
        return _back(secret, to="feeds", error="That is not an http(s) URL.")

    store = _store(request)
    if any(feed.url == target for feed in store.list_feeds(secret)):
        return _back(secret, to="feeds", notice="That feed is already subscribed.")
    if len(store.list_feeds(secret)) >= MAX_FEEDS:
        return _back(secret, to="feeds", error=f"The limit is {MAX_FEEDS} feeds.")

    try:
        target, parsed = await rss.resolve_feed(target)
    except UpstreamError as e:
        return _back(secret, to="feeds", error=f"Could not use that feed: {e}")
    except Exception:
        logger.exception("feed add failed")
        return _back(secret, to="feeds", error="Could not read that feed.")

    if any(feed.url == target for feed in store.list_feeds(secret)):
        return _back(secret, to="feeds", notice="That feed is already subscribed.")
    subscribe(request, secret, target, parsed)
    return _back(secret, to="feeds", notice=f"Added {parsed.title or target}.")


@router.post("/{secret}/feeds/refresh")
async def refresh_feeds(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    reader = await _reader(request, secret)
    try:
        await asyncio.wait_for(reader.sources["rss"].refresh(force=True), FEEDS_REFRESH_TIMEOUT)
    except TimeoutError:
        return _back(secret, to="feeds", error="Some feeds are slow to answer; try again shortly.")
    failing = sum(1 for feed in _store(request).list_feeds(secret) if feed.last_error)
    if failing:
        return _back(secret, to="feeds", error=f"Checked. {failing} failing, marked below.")
    return _back(secret, to="feeds", notice="Checked. All feeds answered.")


@router.post("/{secret}/feeds/import")
async def import_feeds(
    secret: str, request: Request, csrf: str = Form(""), file: UploadFile = File(...)
):
    """Subscribe to everything in an OPML file, unchecked.

    Not fetched one by one here: a large export would hold the request for
    minutes. Each feed's health shows up after the next fetch."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    document = await file.read(MAX_OPML_BYTES + 1)
    if len(document) > MAX_OPML_BYTES:
        return _back(secret, to="feeds", error="That file is too large to be a feed list.")
    try:
        outlines = rss.parse_opml(document)
    except UpstreamError as e:
        return _back(secret, to="feeds", error=str(e))

    store = _store(request)
    known = {feed.url for feed in store.list_feeds(secret)}
    added = 0
    for raw_url, title in outlines:
        url = _valid_url(raw_url)
        if url is None or url in known:
            continue
        if len(known) >= MAX_FEEDS:
            return _back(
                secret, to="feeds", error=f"Imported {added}, then reached the {MAX_FEEDS} feed limit."
            )
        store.add_feed(secret, url, title[:120])
        known.add(url)
        added += 1
    if not added:
        return _back(secret, to="feeds", notice="Nothing new in that file.")
    _ensure_delivery(request, secret)
    return _back(secret, to="feeds", notice=f"Imported {_count(added, 'feed')}.")


@router.get("/{secret}/feeds.opml")
async def export_feeds(secret: str, request: Request):
    _resolve(secret, request)
    return Response(
        rss.to_opml(_store(request).list_feeds(secret)),
        media_type="text/x-opml",
        headers={"Content-Disposition": 'attachment; filename="matins-feeds.opml"'},
    )


@router.get("/{secret}/feeds/{feed_id}", response_class=HTMLResponse)
async def feed_detail(
    secret: str,
    feed_id: str,
    request: Request,
    notice: str | None = None,
    error: str | None = None,
):
    """One source, and for each recent article why it is or is not in the issue."""
    _resolve(secret, request)
    store = _store(request)
    feed = store.get_feed(secret, feed_id)
    if feed is None:
        raise HTTPException(404, "You are not subscribed to that feed.")
    reader = await _reader(request, secret)
    try:
        articles, _ = await reader.sources["rss"].list_articles(feed_id)
    except UpstreamError:
        articles = []
    candidates = await _service(request).plan(secret, reader)
    limits = limits_for(store.get_delivery(secret))
    keys = [reader.decorate_id("rss", a.id) for a in articles]
    printed = store.issued_articles(secret, keys)
    skipped = store.skipped_ids(secret)
    planned = {reader.decorate_id(c.source, c.article.id) for c in candidates}
    now = datetime.now(UTC)
    rows = [
        {
            "id": key,
            "title": article.title,
            "published": f"{article.content_date:%d %b %Y}" if article.content_date else "",
            "state": _article_state(key, article, now, limits, printed, skipped, planned),
        }
        for key, article in list(zip(keys, articles, strict=True))[:30]
    ]
    return _html(
        pages.feed_page(
            secret=secret,
            csrf=_csrf_token(secret),
            feed=_feed_row(store.get_feed(secret, feed_id)),
            items=rows,
            account_voice=_account_voice(store.get_delivery(secret)),
            listening=config.get_tts_assets_dir() is not None,
            window_hours=limits.window_hours,
            max_per_feed=limits.max_per_feed,
            notice=notice,
            error=error,
        )
    )


@router.post("/{secret}/feeds/{feed_id}/rename")
async def rename_feed(
    secret: str, feed_id: str, request: Request, csrf: str = Form(""), title: str = Form("")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    store = _store(request)
    if store.get_feed(secret, feed_id) is None:
        raise HTTPException(404, "No such feed")
    name = " ".join(title.split())[:120]
    if not name:
        return _back(secret, to=f"feeds/{feed_id}", error="A feed needs a name.")
    store.rename_feed(secret, feed_id, name)
    return _back(secret, to=f"feeds/{feed_id}", notice="Renamed.")


@router.post("/{secret}/feeds/{feed_id}/voice")
async def feed_voice(
    secret: str, feed_id: str, request: Request, csrf: str = Form(""), voice: str = Form("")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    chosen = voices.valid_voice(voice)
    if voice.strip() and chosen is None:
        return _back(secret, to=f"feeds/{feed_id}", error="Unknown voice.")
    if not _store(request).set_feed_voice(secret, feed_id, chosen):
        raise HTTPException(404, "No such feed")
    return _back(secret, to=f"feeds/{feed_id}", notice="Voice saved.")


@router.post("/{secret}/feeds/{feed_id}/pause")
async def pause_feed(
    secret: str, feed_id: str, request: Request, csrf: str = Form(""), paused: str = Form("1")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    pausing = paused == "1"
    if not _store(request).set_feed_paused(secret, feed_id, pausing):
        raise HTTPException(404, "No such feed")
    return _back(
        secret,
        to=f"feeds/{feed_id}",
        notice="Paused: left out of issues." if pausing else "Back in your issues.",
    )


@router.post("/{secret}/feeds/{feed_id}/auth")
async def feed_auth(
    secret: str,
    feed_id: str,
    request: Request,
    csrf: str = Form(""),
    cookie: str = Form(""),
):
    """Store the subscriber cookie used to fetch this feed's article pages."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    value = cookie.strip()
    if value == "-":
        value = ""
    if len(value.encode()) > 8192:
        return _back(secret, to=f"feeds/{feed_id}", error="That cookie is too long.")
    if "\n" in value or "\r" in value:
        return _back(
            secret, to=f"feeds/{feed_id}", error="Paste the Cookie header on one line."
        )
    if not _store(request).set_feed_auth(secret, feed_id, value or None):
        raise HTTPException(404, "No such feed")
    return _back(
        secret,
        to=f"feeds/{feed_id}",
        notice="Subscriber cookie saved." if value else "Subscriber cookie removed.",
    )


@router.post("/{secret}/feeds/{feed_id}/delete")
async def remove_feed(secret: str, feed_id: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not _store(request).delete_feed(secret, feed_id):
        raise HTTPException(404, "No such feed")
    return _back(secret, to="feeds", notice="Feed removed.")


@router.post("/{secret}/saved/{saved_id}/delete")
async def remove_saved(
    secret: str, saved_id: int, request: Request, csrf: str = Form(""), back: str = Form("home")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not _store(request).delete_saved(secret, saved_id):
        raise HTTPException(404, "No such saved link")
    return _back(secret, to=BACK_TARGETS.get(back, ""), notice="Removed from the queue.")


@router.post("/{secret}/forget")
async def forget_catalogue(secret: str, request: Request, csrf: str = Form("")):
    """Drop this catalogue from the browser's list. Not a deletion."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    response = RedirectResponse("/", status_code=303)
    catalogues.forget(secret, request, response)
    return response


@router.post("/{secret}/save")
async def save(secret: str, request: Request):
    _resolve(secret, request)
    url = ""
    title = None
    back = "home"
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        url = str(body.get("url") or "")
        title = body.get("title")
        title = str(title)[:300] if title else None
    else:
        form = await request.form()
        url = str(form.get("url") or "")
        title = form.get("title")
        title = str(title)[:300] if title else None
        back = str(form.get("back") or "home")

    url = _valid_url(url)
    if not url:
        raise HTTPException(400, "Expected an http(s) url")
    queued = _store(request).save_url(secret, url, title)
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(
            {"queued": queued, "url": url}, status_code=201 if queued else 200
        )
    return _back(
        secret,
        to=BACK_TARGETS.get(back, ""),
        notice="Saved for the next issue." if queued else "Already saved.",
    )


# ------------------------------------------------------------------- reader


@router.get("/{secret}/read", response_class=HTMLResponse)
async def reader_index(
    secret: str,
    request: Request,
    article: str | None = None,
    view: str = "all",
    source: str | None = None,
    issue: str | None = None,
    notice: str | None = None,
):
    """The mailbox: everything the catalogue can show, with one article open.

    The issue is curated; this list is not. Feeds, the saved queue and curator
    candidates (Bluesky) all appear here, and the two buttons on each row are the
    only way an item moves from "all the news" into "the next issue"."""
    _resolve(secret, request)
    store = _store(request)
    reader = await _reader(request, secret)
    stream = await _reader_items(request, secret)
    delivery = _store(request).get_delivery(secret)
    show_dates = delivery.show_dates if delivery else True
    timezone = _timezone(request, secret)
    read = store.read_ids(secret, [entry[0] for entry in stream])
    urls = [getattr(entry[1], "url", "") or "" for entry in stream]
    ratings = store.signals_for(secret, [url for url in urls if url])
    queued = {item.url: item for item in store.list_saved(secret)}

    items: list[dict] = []
    for item_id, source_article, section, kind in stream:
        url = getattr(source_article, "url", "") or ""
        saved = queued.get(url)
        items.append(
            {
                "id": item_id,
                "title": source_article.title,
                "source": section,
                "url": url,
                "kind": kind,
                "signal_source": getattr(source_article, "signal_source", section),
                "saved": saved is not None,
                "saved_id": saved.id if saved else None,
                "rating": ratings.get(url, 0),
                # A feed summary's length says nothing about the article. Until
                # the body is fetched, only a credible count gets an estimate.
                "minutes": _reading_minutes(
                    getattr(source_article, "word_count", None) or 0
                ),
                # What the publisher states, or nothing. Never the time it was
                # queued or fetched.
                "published": (
                    published_label(getattr(source_article, "published", None), timezone)
                    if show_dates
                    else ""
                ),
                "read": item_id in read,
                "selected": False,
            }
        )

    # An issue view is a fixed edition, not the live stream: keep its order and
    # show items even if the feed has rolled over since (the queue's consumed
    # links are gone from the stream; the issue still remembers them).
    issue_obj = store.get_issue(secret, issue) if issue else None
    entries = store.issue_entries(secret, issue) if issue_obj else []
    if issue_obj and entries:
        by_id = {item["id"]: item for item in items}
        items = [
            by_id.get(entry.article_id)
            or _issue_item(entry, ratings, queued, read)
            for entry in entries
        ]
    else:
        issue = None

    archived_rows = store.list_archived(secret, limit=MAX_READER_ITEMS)
    archived_urls = store.archived_urls(secret, [item["url"] for item in items if item["url"]])
    for item in items:
        # Printing archives an article, so an edition's own view must not show
        # its articles as dismissed: they stay likeable there.
        item["archived"] = not issue and bool(item["url"]) and item["url"] in archived_urls
    visible = items if issue else [item for item in items if not item["archived"]]
    names = {_source_key(item) for item in visible} | {
        _archived_key(row) for row in archived_rows
    }
    # Bluesky is one source, not one per author: the whole point of following
    # people is reading them together.
    sources = [] if issue else sorted(
        names, key=lambda name: (name != "Bluesky", name.casefold())
    )
    source = None if issue else (source or None)
    if source:
        visible = [item for item in visible if _source_key(item) == source]
        archived_rows = [row for row in archived_rows if _archived_key(row) == source]

    counts = {
        "all": len(visible),
        "unread": sum(1 for item in visible if not item["read"]),
        "liked": sum(1 for item in visible if item["rating"] > 0),
        "queued": sum(1 for item in visible if item["saved"]),
        "archived": store.count_archived(secret) if not source else len(archived_rows),
    }
    view = view if view in counts else "all"
    if view == "archived":
        shown = [_archived_item(row) for row in archived_rows]
    else:
        shown = _filter_reader(visible, view)

    selected = (
        next((item for item in visible if item["id"] == article), None)
        if view != "archived"
        else None
    )
    body: str | None = None
    error: str | None = None
    if selected is not None:
        try:
            body = await _reader_body(request, secret, reader, selected)
        except (ArticleUnavailable, UpstreamError, KeyError) as e:
            error = getattr(e, "message", None) or "Could not load that article."
        if body is None and error is None:
            error = "Could not render that article."
        if body is not None:
            if words := extract.word_count(body):
                selected["minutes"] = _reading_minutes(words)
            store.mark_read(secret, selected["id"])
            selected["read"] = True
            if selected["url"]:
                # Opening it is interest, weaker than a like but real.
                store.record_event(
                    secret,
                    selected["url"],
                    article_id=selected["id"],
                    title=selected["title"],
                    source=selected["signal_source"],
                    kind="read",
                )
            # Keep it in the list even when the current filter would hide it:
            # a click must not make the row vanish under the pointer.
            if selected not in shown:
                shown = [selected, *shown[: MAX_READER_ITEMS - 1]]
        selected["selected"] = True

    return _html(
        pages.reader_page(
            secret=secret,
            csrf=_csrf_token(secret),
            items=shown[:MAX_READER_ITEMS],
            counts=counts,
            view=view,
            sources=sources,
            source=source,
            issue=issue,
            issue_title=issue_obj.title if issue_obj else None,
            selected=selected,
            body=body,
            voice=_listen_voice(store, secret, selected, delivery) if body else None,
            list_voice=_listen_voice(store, secret, {}, delivery),
            notice=notice,
            error=error,
        ),
        csp=READER_CSP,
        isolated=True,
    )


def _listen_voice(store, secret: str, item: dict, delivery) -> str | None:
    """The voice to read this article with, or None when listening is off.

    A feed's own voice wins (Il Post reads in Italian), then the catalogue's.
    Saved links and Bluesky posts have no feed, so they use the catalogue's."""
    if config.get_tts_assets_dir() is None:
        return None
    fallback = (delivery.voice if delivery else None) or voices.DEFAULT_VOICE
    item_id = item.get("id") or ""
    # By id, not kind: an issue's row for a feed article has kind "issue".
    if item_id.startswith("feeds:"):
        feed_id, _, _digest = item_id.removeprefix("feeds:").rpartition("-")
        feed = store.get_feed(secret, feed_id) if feed_id else None
        if feed is not None and feed.voice:
            return feed.voice
    return fallback


def _issue_item(entry, ratings: dict[str, int], queued: dict, read: set[str]) -> dict:
    url = entry.url or ""
    saved = queued.get(url)
    return {
        "id": entry.article_id,
        "title": entry.title,
        "source": entry.section,
        "signal_source": entry.section,
        "url": url,
        "kind": "issue",
        "saved": saved is not None,
        "saved_id": saved.id if saved else None,
        "rating": ratings.get(url, 0),
        "minutes": 0,
        "read": entry.article_id in read,
        "selected": False,
        "archived": False,
    }


def _source_key(item: dict) -> str:
    return "Bluesky" if item.get("kind") == "candidate" else item["source"]


def _archived_key(row) -> str:
    return "Bluesky" if row.kind == "candidate" else (row.source or "Archived")


def _archived_item(row) -> dict:
    display_source = row.source or "Archived"
    if row.kind == "candidate" and row.source:
        display_source = f"Bluesky · {row.source}"
    return {
        "id": row.id,
        "title": row.title or row.url,
        "source": display_source,
        "url": row.url,
        "kind": "archived",
        "saved": False,
        "saved_id": None,
        "rating": 0,
        "minutes": 1,
        "read": True,
        "selected": False,
        "archived": True,
    }


def _reading_minutes(words: int) -> int:
    """Minutes at 250 wpm, or 0 when the count is too small to be the article.

    A 40-word RSS summary is not a 1-minute article; showing "1 minute" on every
    row was a lie the feed made easy."""
    return max(1, round(words / 250)) if words >= MIN_KNOWN_WORDS else 0


def _filter_reader(items: list[dict], view: str) -> list[dict]:
    if view == "unread":
        return [item for item in items if not item["read"]]
    if view == "liked":
        return [item for item in items if item["rating"] > 0]
    if view == "queued":
        return [item for item in items if item["saved"]]
    return items


async def _reader_body(request, secret: str, reader, item: dict) -> str | None:
    if item["kind"] == "candidate":
        candidate = _store(request).get_candidate(secret, item["id"].split(":", 1)[1])
        if candidate is None:
            return None
        content = candidate.content or candidate.summary
        # A post that only points at an article ("Title | Discussion <link>")
        # shows the article, as the issue prints it.
        linked = await linked_content(content) if content else None
        return _drop_repeated_title(
            to_page_html(linked) if linked else _candidate_html(content), item["title"]
        )
    _article, raw = await reader.get_article_html(item["id"])
    return _drop_repeated_title(to_page_html(raw), item["title"])


def _drop_repeated_title(body: str | None, title: str) -> str | None:
    """The body without a first line that only repeats the pane's heading.

    A Bluesky bot's post is its own title, and many pages open with their <h1>:
    either way the reader would see the title twice, the second time as the
    whole first paragraph."""
    if not body or not title:
        return body
    try:
        root = fragment_fromstring(body, create_parent="div")
    except (ValueError, TypeError):
        return body
    first = next((el for el in root if isinstance(el.tag, str)), None)
    if first is None or first.tag not in ("h1", "h2", "h3", "p"):
        return body

    def norm(text: str) -> str:
        return " ".join(text.replace("|", " ").split()).casefold()

    text, heading = norm(first.text_content()), norm(title)
    # The bot's line carries a "Discussion" anchor after the title.
    text = text.removesuffix(" discussion").strip()
    heading = heading.removesuffix(" discussion").strip()
    if not text or text != heading:
        return body
    if first.findall(".//img"):
        return body
    root.remove(first)
    rest = ((root.text or "").strip()) + "".join(tostring(el, encoding="unicode") for el in root)
    # A short post is its own title: dropping it would leave nothing to read.
    return rest if extract.word_count(rest) else body


def _candidate_html(content: str) -> str:
    """A candidate arrives as HTML from the curator; plain text is a fallback."""
    if "<" in content:
        return content
    return "".join(
        f"<p>{html_escape(paragraph)}</p>"
        for paragraph in content.split("\n\n")
        if paragraph.strip()
    )


def _read_href(
    secret: str,
    view: str,
    article: str | None = None,
    notice: str | None = None,
    source: str | None = None,
    issue: str | None = None,
) -> str:
    target = f"/{secret}/read?view={quote(view)}"
    if article:
        target += f"&article={quote(article, safe='')}"
    if source:
        target += f"&source={quote(source)}"
    if issue:
        target += f"&issue={quote(issue)}"
    if notice:
        target += f"&notice={quote(notice)}"
    return target


async def _find_reader_item(
    request, secret: str, article_id: str
) -> dict | None:
    for item_id, article, section, kind in await _reader_items(request, secret):
        if item_id != article_id:
            continue
        return {
            "id": item_id,
            "title": article.title,
            "url": getattr(article, "url", "") or "",
            "source": getattr(article, "signal_source", section),
            "kind": kind,
        }
    # An issue keeps items the live stream no longer lists (a consumed saved
    # link, a feed that rolled over). The action buttons still work on them.
    entry = _store(request).find_issue_item(secret, article_id)
    if entry is None:
        return None
    return {
        "id": entry.article_id,
        "title": entry.title,
        "url": entry.url or "",
        "source": entry.section,
        "kind": "issue",
    }


# What "Listen to all" reads: the same blocks the page player highlights.
_SPOKEN = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption")


def _spoken_blocks(body: str) -> list[str]:
    try:
        root = fragment_fromstring(body, create_parent="div")
    except (ValueError, TypeError):
        return []
    blocks = []
    for el in root.iter(*_SPOKEN):
        if any(True for _ in el.iterdescendants(*_SPOKEN)):
            continue
        if any(a.tag == "pre" for a in el.iterancestors()):
            continue
        text = " ".join(el.text_content().split())
        if text:
            blocks.append(text)
    return blocks


@router.get("/{secret}/read/listen")
async def reader_listen(secret: str, request: Request, article: str):
    """One article as text blocks, title first, for "Listen to all".

    The page fetches these one after another while the previous article plays.
    Listening is reading: the article is marked read."""
    _resolve(secret, request)
    if config.get_tts_assets_dir() is None:
        raise HTTPException(404, "Listening is off")
    item = await _find_reader_item(request, secret, article)
    if item is None:
        raise HTTPException(404, "No such article")
    reader = await _reader(request, secret)
    try:
        body = await _reader_body(request, secret, reader, item)
    except (ArticleUnavailable, UpstreamError, KeyError):
        body = None
    store = _store(request)
    blocks = _spoken_blocks(body) if body else []
    if blocks:
        store.mark_read(secret, item["id"])
    return JSONResponse(
        {
            "id": item["id"],
            "title": item["title"],
            "voice": _listen_voice(store, secret, item, store.get_delivery(secret)),
            "blocks": [item["title"], *blocks] if blocks else [],
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{secret}/read/open")
async def reader_open(secret: str, request: Request, article: str):
    """Hand the reader to the publisher, and count it as interest.

    A GET redirect rather than a form: the link stays a link (middle-click, new
    tab). The destination comes from the catalogue item, never from the query.
    """
    _resolve(secret, request)
    item = await _find_reader_item(request, secret, article)
    if item is None or not item["url"]:
        raise HTTPException(404, "No such article")
    _store(request).record_event(
        secret,
        item["url"],
        article_id=item["id"],
        title=item["title"],
        source=item["source"],
        kind="opened",
    )
    return RedirectResponse(item["url"], status_code=302)


@router.post("/{secret}/read/feedback")
async def reader_feedback(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article_id: str = Form(""),
    value: str = Form("like"),
    view: str = Form("all"),
    source: str = Form(""),
    issue: str = Form(""),
    selected: str = Form(""),
    from_list: str = Form(""),
):
    """Like / not for me, keyed by URL so the curator can rank next morning."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    item = await _find_reader_item(request, secret, article_id)
    if item is None or not item["url"]:
        raise HTTPException(404, "No such article")
    store = _store(request)
    rating = {"like": 1, "dislike": -1}.get(value, 0)
    if rating:
        store.set_signal(
            secret,
            item["url"],
            rating,
            article_id=item["id"],
            title=item["title"],
            source=item["source"],
        )
    else:
        store.clear_signal(secret, item["url"])
    return RedirectResponse(_read_href(secret, view, (selected or None) if from_list else article_id, source=source or None, issue=issue or None), status_code=303)


@router.post("/{secret}/read/queue")
async def reader_queue(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article_id: str = Form(""),
    view: str = Form("all"),
    source: str = Form(""),
    issue: str = Form(""),
    selected: str = Form(""),
    from_list: str = Form(""),
):
    """Add any browsable item to the curated next issue."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    item = await _find_reader_item(request, secret, article_id)
    if item is None or not item["url"]:
        raise HTTPException(404, "No such article")
    content = None
    candidate = None
    if item["kind"] == "candidate":
        candidate = _store(request).get_candidate(secret, article_id.split(":", 1)[1])
        content = candidate.content if candidate else None
    reader = await _reader(request, secret)
    lede = await reader.sources["later"].lede_for(item["url"])
    published = candidate.published if item["kind"] == "candidate" and candidate else None
    if published is None and item["kind"] != "candidate":
        date = await reader.sources["rss"].published_for(item["url"])
        published = date.replace(tzinfo=UTC).timestamp() if date else None
    _store(request).save_url(
        secret, item["url"], item["title"], content=content, lede=lede, published=published
    )
    return RedirectResponse(_read_href(secret, view, (selected or None) if from_list else article_id, source=source or None, issue=issue or None), status_code=303)


@router.post("/{secret}/read/archive")
async def reader_archive(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article_id: str = Form(""),
    view: str = Form("all"),
    source: str = Form(""),
    issue: str = Form(""),
):
    """Discard an item: hide it from the reader, tell the curator, unqueue it."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    item = await _find_reader_item(request, secret, article_id)
    if item is None or not item["url"]:
        raise HTTPException(404, "No such article")
    store = _store(request)
    store.archive_article(
        secret,
        item["url"],
        article_id=item["id"],
        title=item["title"],
        source=item["source"],
        kind=item["kind"],
    )
    store.set_signal(
        secret,
        item["url"],
        -1,
        article_id=item["id"],
        title=item["title"],
        source=item["source"],
    )
    queued = next(
        (saved for saved in store.list_saved(secret) if saved.url == item["url"]), None
    )
    if queued is not None:
        store.delete_saved(secret, queued.id)
    # Do not select it: the row just left this view.
    target = "all" if view == "archived" else view
    return RedirectResponse(
        _read_href(
            secret,
            target,
            notice="Archived. It is under the Archived tab if you want it back.",
            source=source or None,
            issue=issue or None,
        ),
        status_code=303,
    )


@router.post("/{secret}/read/archive-all")
async def reader_archive_all(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    keep: str = Form("none"),
    source: str = Form(""),
):
    """Archive everything in the list at once, optionally keeping the liked ones.

    Housekeeping, not feedback: no "not for me" signal is recorded, so a
    curator does not learn that 700 unread articles were all unwanted. Items
    queued for the next issue are left alone; unqueue them first if wanted."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    store = _store(request)
    stream = await _reader_items(request, secret)
    urls = [getattr(entry[1], "url", "") or "" for entry in stream]
    liked = {url for url, rating in store.signals_for(secret, [u for u in urls if u]).items() if rating > 0}
    queued = {item.url for item in store.list_saved(secret)}
    rows = []
    for item_id, article, section, kind in stream:
        url = getattr(article, "url", "") or ""
        if not url or url in queued or (keep == "liked" and url in liked):
            continue
        name = "Bluesky" if kind == "candidate" else section
        if source and name != source:
            continue
        rows.append(
            {
                "url": url,
                "article_id": item_id,
                "title": article.title,
                "source": getattr(article, "signal_source", section),
                "kind": kind,
            }
        )
    count = store.archive_many(secret, rows)
    kept = " Liked and queued articles were kept." if keep == "liked" else " Queued articles were kept."
    return RedirectResponse(
        _read_href(
            secret,
            "all",
            notice=f"Archived {count} article{'s' if count != 1 else ''}.{kept}",
            source=source or None,
        ),
        status_code=303,
    )


@router.post("/{secret}/read/restore")
async def reader_restore(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article_id: str = Form(""),
    view: str = Form("archived"),
    source: str = Form(""),
    issue: str = Form(""),
):
    """Bring an archived item back and clear the not-interested mark."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    store = _store(request)
    archived = store.get_archived(secret, article_id)
    if archived is None:
        raise HTTPException(404, "No such archived article")
    store.unarchive_article(secret, archived.url)
    store.clear_signal(secret, archived.url)
    target = "all" if view == "archived" else view
    return RedirectResponse(
        _read_href(
            secret,
            target,
            notice="Restored to the reader.",
            source=source or None,
            issue=issue or None,
        ),
        status_code=303,
    )


@router.post("/{secret}/read/unqueue")
async def reader_unqueue(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article_id: str = Form(""),
    view: str = Form("all"),
    source: str = Form(""),
    issue: str = Form(""),
    selected: str = Form(""),
    from_list: str = Form(""),
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    item = await _find_reader_item(request, secret, article_id)
    if item is None or not item["url"]:
        raise HTTPException(404, "No such article")
    store = _store(request)
    queued = next(
        (saved for saved in store.list_saved(secret) if saved.url == item["url"]), None
    )
    if queued is not None:
        store.delete_saved(secret, queued.id)
    return RedirectResponse(_read_href(secret, view, (selected or None) if from_list else article_id, source=source or None, issue=issue or None), status_code=303)


@router.post("/{secret}/read/mark-all")
async def reader_mark_all(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    stream = await _reader_items(request, secret)
    for item_id, _article, _section, _source in stream:
        _store(request).mark_read(secret, item_id)
    return RedirectResponse(f"/{secret}/read", status_code=303)


@router.get("/{secret}/read/{article_id:path}", response_class=HTMLResponse)
async def reader_article(secret: str, article_id: str, request: Request):
    _resolve(secret, request)
    reader = await _reader(request, secret)
    try:
        article, raw = await reader.get_article_html(article_id)
    except ArticleUnavailable as e:
        raise HTTPException(e.status, str(e)) from None
    except (UpstreamError, KeyError):
        raise HTTPException(404, "Could not load that article") from None

    body = to_page_html(raw)
    if body is None:
        raise HTTPException(422, "Could not render that article")
    store = _store(request)
    store.mark_read(secret, article_id)
    found = await _find_reader_item(request, secret, article_id)
    if article.url:
        store.record_event(
            secret,
            article.url,
            article_id=article_id,
            title=article.title,
            source=found["source"] if found else "",
            kind="read",
        )
    return _html(
        pages.reader_article_page(
            secret=secret,
            csrf=_csrf_token(secret),
            article_id=article_id,
            title=article.title,
            author=article.author,
            source_url=article.url,
            body=body,
            minutes=_reading_minutes(extract.word_count(body)),
        ),
        csp=READER_CSP,
    )


@router.post("/{secret}/read/{article_id:path}/unread")
async def reader_mark_unread(
    secret: str,
    article_id: str,
    request: Request,
    csrf: str = Form(""),
    view: str = Form("all"),
    source: str = Form(""),
    issue: str = Form(""),
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    _store(request).mark_unread(secret, article_id)
    return RedirectResponse(
        _read_href(secret, view, source=source or None, issue=issue or None), status_code=303
    )


async def _reader_items(request: Request, secret: str) -> list[tuple[str, object, str, str]]:
    """Every item the catalogue can offer, as (catalogue id, article, source, kind).

    Union of the saved queue and every feed, each item carrying the id the
    download route uses, so read state and the reader link agree with OPDS."""
    reader = await _reader(request, secret)
    items: list[tuple[str, object, str, str]] = []

    queue = reader.sources.get("later")
    if queue is not None:
        try:
            saved, _ = await queue.list_articles("inbox")
        except (UpstreamError, KeyError):
            saved = []
        items.extend(
            (reader.decorate_id("later", a.id), a, "Saved for later", "later")
            for a in saved
        )

    feeds = reader.sources.get("rss")
    if feeds is not None:
        try:
            folders = await feeds.list_folders()
        except UpstreamError:
            folders = []
        for folder in folders:
            try:
                articles, _ = await feeds.list_articles(folder.id)
            except UpstreamError as e:
                logger.info("reader: feed %s unavailable: %s", folder.title, e)
                continue
            stored = _store(request).get_feed(secret, folder.id)
            section = (stored.title if stored else "") or folder.title
            items.extend(
                (reader.decorate_id("rss", a.id), a, section, "rss") for a in articles
            )

    # Curator candidates (Bluesky): browsable here, printable only when queued.
    for candidate in _store(request).list_candidates(secret, limit=MAX_READER_ITEMS):
        stamp = candidate.published or candidate.added_at
        article = SimpleNamespace(
            title=candidate.title or candidate.url,
            url=candidate.url,
            word_count=max(1, len((candidate.content or candidate.summary).split())),
            content_date=datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None),
            signal_source=candidate.source,
            published=(
                datetime.fromtimestamp(candidate.published, UTC).replace(tzinfo=None)
                if candidate.published
                else None
            ),
        )
        section = (
            f"Bluesky · {candidate.source}" if candidate.source else "Bluesky"
        )
        items.append((f"cand:{candidate.id}", article, section, "candidate"))

    # One row per URL. A queued feed article exists twice (the feed's copy and
    # the queue's), and the queue copy carries the save time as its date.
    # Prefer the source copy: the row keeps its real origin, the feed's article
    # id stays stable across a like/queue click, and "queued" is still shown
    # from the saved table.
    unique: dict[str, tuple[str, object, str, str]] = {}
    for entry in items:
        key = getattr(entry[1], "url", "") or entry[0]
        current = unique.get(key)
        if current is None or (current[3] == "later" and entry[3] != "later"):
            unique[key] = entry
    # Sort *after* collapsing: queueing must not move a row. Dating the dedupe
    # before the sort would pin every newly queued article to the top of the
    # list, because its queue copy is dated "now".
    return sorted(
        unique.values(),
        key=lambda item: item[1].content_date or datetime.min,
        reverse=True,
    )


# --------------------------------------------------------------- settings


@router.get("/{secret}/settings", response_class=HTMLResponse)
async def settings_get(
    secret: str,
    request: Request,
    saved: int = 0,
    notice: str | None = None,
    error: str | None = None,
):
    _resolve(secret, request)
    return _html(
        _render_settings(
            request, secret, notice="Settings saved." if saved else notice, error=error
        )
    )


@router.post("/{secret}/settings")
async def settings_post(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    email: str = Form(""),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    schedule: str = Form(""),
    timezone: str = Form(""),
    enabled: str = Form(""),
    max_articles: str = Form(""),
    max_per_feed: str = Form(""),
    max_words: str = Form(""),
    window_hours: str = Form(""),
    max_saved: str = Form(""),
    auto_select: str = Form("1"),
    show_dates: str = Form(""),
    voice: str = Form(""),
):
    """Schedule, limits and notifications. Sources have their own page."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)

    curated = auto_select == "0"
    overrides = {
        "email": email,
        "webhook_url": webhook_url,
        "schedule": schedule,
        "timezone": timezone,
        "enabled": bool(enabled),
        "auto_select": not curated,
        "show_dates": bool(show_dates),
        "voice": voice,
        "limits": {
            "max_articles": max_articles,
            "max_per_feed": max_per_feed,
            "max_words": max_words,
            "window_hours": window_hours,
            "max_saved": max_saved,
        },
    }
    schedule = _normalize_schedule(schedule) or config.get_default_schedule()
    timezone = (timezone or "").strip() or config.get_default_timezone()
    overrides["schedule"] = schedule
    overrides["timezone"] = timezone

    if not _valid_timezone(timezone):
        return _html(
            _render_settings(
                request, secret, error=f"Unknown timezone: {timezone}", overrides=overrides
            ),
            status=400,
        )
    if voice.strip() and voices.valid_voice(voice) is None:
        return _html(
            _render_settings(request, secret, error="Unknown voice.", overrides=overrides),
            status=400,
        )
    if webhook_url.strip() and not _valid_url(webhook_url):
        return _html(
            _render_settings(
                request,
                secret,
                error="The webhook must be an http(s) URL.",
                overrides=overrides,
            ),
            status=400,
        )

    try:
        limits = {key: _parse_limit(key, value) for key, value in overrides["limits"].items()}
    except ValueError as e:
        return _html(
            _render_settings(request, secret, error=str(e), overrides=overrides), status=400
        )

    address = _normalize_email(email) if email.strip() else None
    if email.strip() and address is None:
        return _html(
            _render_settings(
                request,
                secret,
                error="That does not look like an email address.",
                overrides=overrides,
            ),
            status=400,
        )
    if address is not None:
        try:
            _store(request).link_email(secret, address)
        except ValueError as e:
            return _html(
                _render_settings(request, secret, error=str(e).capitalize(), overrides=overrides),
                status=400,
            )
    else:
        _store(request).unlink_email(secret)

    previous = _store(request).get_delivery(secret)
    _store(request).set_delivery(
        secret,
        webhook_url=webhook_url,
        webhook_secret=_secrets_unchanged(previous, webhook_secret),
        email=address or "",
        schedule=schedule,
        timezone=timezone,
        enabled=bool(enabled),
    )
    _store(request).set_issue_limits(secret, **limits)
    _store(request).set_auto_select(secret, not curated)
    _store(request).set_show_dates(secret, bool(show_dates))
    _store(request).set_voice(secret, voices.valid_voice(voice))
    return RedirectResponse(f"/{secret}/settings?saved=1", status_code=303)


@router.post("/{secret}/schedule")
async def set_schedule(
    secret: str, request: Request, csrf: str = Form(""), enabled: str = Form("1")
):
    """The one-click switch on the home page."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    _ensure_delivery(request, secret)
    on = enabled == "1"
    _store(request).set_enabled(secret, on)
    return _back(secret, notice="Issues will arrive every morning." if on else "Automatic issues off.")


@router.post("/{secret}/settings/test-email")
async def test_email(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    store = _store(request)
    address = store.email_for_secret(secret)
    if not address:
        return _back(secret, to="settings", error="Save an email address first.")
    sample = SimpleNamespace(
        id="test", title="Matins — test notification", article_count=0, word_count=0
    )
    if await notify.send_issue_notification(
        address, sample, f"{app_config.get_base_url()}/{secret}/issues/"
    ):
        return _back(secret, to="settings", notice=f"Test email sent to {address}.")
    return _back(secret, to="settings", error="The test email could not be sent.")


@router.post("/{secret}/settings/test-webhook")
async def test_webhook(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    delivery = _store(request).get_delivery(secret)
    if delivery is None or not delivery.webhook_url:
        return _back(secret, to="settings", error="Save a webhook URL first.")
    payload = {"event": "test", "delivered_at": int(datetime.now(UTC).timestamp())}
    if await webhooks.deliver(delivery.webhook_url, delivery.webhook_secret, payload):
        return _back(secret, to="settings", notice="The webhook answered with a success.")
    return _back(
        secret, to="settings", error="The webhook did not accept the test. Check its URL and logs."
    )


# ------------------------------------------------------------------ issues


@router.get("/{secret}/issues/preview", response_class=HTMLResponse)
async def issues_preview(
    secret: str, request: Request, notice: str | None = None, error: str | None = None
):
    """The next issue's selection, editable, without writing anything."""
    _resolve(secret, request)
    store = _store(request)
    reader = await _reader(request, secret)
    only = _issue_only(store, secret)
    candidates = await _service(request).plan(secret, reader, only=only)
    limits = limits_for(store.get_delivery(secret))
    return _html(
        pages.preview_page(
            secret=secret,
            csrf=_csrf_token(secret),
            candidates=[
                {
                    "id": reader.decorate_id(c.source, c.article.id),
                    "title": c.article.title,
                    "section": c.section,
                    "url": c.article.url,
                    "saved_id": c.article.id if c.source == "later" else None,
                    # Only trust a count from a feed that ships the full text; a
                    # summary's length says nothing about the article's.
                    "words": w if (w := c.article.word_count or 0) >= MIN_KNOWN_WORDS else 0,
                }
                for c in candidates
            ],
            skipped=[
                {"id": item.article_id, "title": item.title, "section": item.section}
                for item in store.list_skips(secret, max_age=SKIP_LIST_MAX_AGE)
            ],
            window_hours=limits.window_hours,
            max_articles=limits.max_articles,
            max_per_feed=limits.max_per_feed,
            max_words=limits.max_words,
            schedule_label=_schedule_label(request, secret),
            has_feeds=bool(store.list_feeds(secret)),
            curated=only == "queue",
            notice=notice,
            error=error,
        )
    )


@router.post("/{secret}/issues/skip")
async def skip_article(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    article: str = Form(""),
    title: str = Form(""),
    section: str = Form(""),
    url: str = Form(""),
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not article.startswith("feeds:") or len(article) > 200:
        raise HTTPException(400, "That article cannot be skipped")
    _store(request).skip_article(secret, article, title or "Untitled", section, _valid_url(url))
    return _back(secret, to="issues/preview", notice="Skipped. It stays out of future issues.")


@router.post("/{secret}/issues/skip/undo")
async def unskip_article(
    secret: str, request: Request, csrf: str = Form(""), article: str = Form("")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    _store(request).unskip_article(secret, article)
    return _back(secret, to="issues/preview", notice="Put back, if it is still recent enough.")


@router.post("/{secret}/issues/build", response_class=HTMLResponse)
async def build_now(secret: str, request: Request, csrf: str = Form("")):
    """Compose now. Answers straight away when the build is quick, and otherwise
    hands the browser to a page that polls: a real issue can take a minute."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    task = _composing.get(secret)
    if task is None or task.done():
        task = asyncio.create_task(_compose(request.app, secret))
        _composing[secret] = task
    done, _ = await asyncio.wait({task}, timeout=COMPOSE_WAIT)
    if not done:
        return RedirectResponse(f"/{secret}/issues/composing", status_code=303)
    return _composed(secret)


@router.get("/{secret}/issues/composing", response_class=HTMLResponse)
async def composing(secret: str, request: Request):
    _resolve(secret, request)
    task = _composing.get(secret)
    if task is None:
        return RedirectResponse(f"/{secret}/issues/", status_code=303)
    if not task.done():
        return _html(pages.composing_page(secret=secret))
    return _composed(secret)


@router.api_route("/{secret}/issues/", methods=["GET", "HEAD"])
async def issues_feed(secret: str, request: Request):
    _resolve(secret, request)
    issues = _service(request).issues_for(secret)
    if _wants_html(request):
        return _html(
            pages.issues_page(
                secret=secret,
                csrf=_csrf_token(secret),
                issues=[_issue_row(issue) for issue in issues],
                timezone=_timezone(request, secret),
                retention=config.get_issue_retention(),
                schedule_label=_schedule_label(request, secret),
                queued=len(_store(request).list_saved(secret)),
                failing=[f["title"] for f in _feed_rows(request, secret) if f["error"]],
                notice=request.query_params.get("notice"),
                error=request.query_params.get("error"),
            )
        )
    articles = [
        Article(
            id=issue.id,
            title=issue.title,
            summary=_issue_summary(issue),
            updated=datetime.fromtimestamp(issue.created_at, tz=UTC).replace(tzinfo=None),
            word_count=issue.word_count,
        )
        for issue in issues
    ]
    return _feed_response(
        request, secret, articles, title="Issues", feed_id="issues", epub_base="issues"
    )


@router.get("/{secret}/issues/{issue_id}.epub")
async def issue_epub(secret: str, issue_id: str, request: Request):
    _resolve(secret, request)
    try:
        path = _service(request).issue_path(secret, issue_id)
    except KeyError:
        raise HTTPException(404, "No such issue") from None
    try:
        data = path.read_bytes()
    except OSError:
        # The row exists but the file is gone — a volume that was replaced, or a
        # prune that raced a download. A 410 says "this edition is gone" rather
        # than "your catalogue is broken".
        raise HTTPException(410, "That issue is no longer on disk") from None
    issue = _store(request).get_issue(secret, issue_id)
    filename = f"{(issue.title if issue else 'matins').replace('—', '-')}.epub"
    filename = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in filename)
    return Response(
        data,
        media_type=EPUB_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{secret}/issues/{issue_id}", response_class=HTMLResponse)
async def issue_detail(
    secret: str,
    issue_id: str,
    request: Request,
    notice: str | None = None,
    error: str | None = None,
):
    _resolve(secret, request)
    store = _store(request)
    issue = store.get_issue(secret, issue_id)
    if issue is None:
        raise HTTPException(404, "That issue no longer exists.")
    return _html(
        pages.issue_page(
            secret=secret,
            csrf=_csrf_token(secret),
            issue=_issue_row(issue),
            entries=[
                {"title": entry.title, "url": entry.url, "section": entry.section}
                for entry in store.issue_entries(secret, issue_id)
            ],
            timezone=_timezone(request, secret),
            notice=notice,
            error=error,
        )
    )


@router.post("/{secret}/issues/{issue_id}/delete")
async def delete_issue(secret: str, issue_id: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not _service(request).delete(secret, issue_id):
        raise HTTPException(404, "That issue no longer exists.")
    return _back(secret, to="issues/", notice="Issue deleted. Its articles are back in line.")


# --------------------------------------------------------------------- admin


@router.post("/admin/issues/run")
async def admin_run(request: Request, secret: str | None = None, force: int = 0):
    token = config.get_admin_token()
    if not token:
        raise HTTPException(404)  # no admin surface unless explicitly enabled
    provided = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(provided.encode(), token.encode()):
        raise HTTPException(403, "Bad admin token")

    store = _store(request)
    targets = [secret] if secret else [d.secret for d in store.list_deliveries()]
    built: list[str] = []
    for target in targets:
        delivery = store.get_delivery(target)
        day = local_day(delivery.timezone if delivery else config.get_default_timezone())
        if not force and store.has_issue_for_day(target, day):
            continue
        issue = await _build(request, target, day)
        if issue is None:
            continue
        built.append(issue.id)
        await _service(request).deliver(target, issue)
    return {"built": built}


# -------------------------------------------------------------------- shared


async def _reader(request: Request, secret: str):
    try:
        return await request.app.state.tenant_reader(secret)
    except KeyError:
        raise HTTPException(404, "Unknown catalogue") from None


def _issue_only(store, secret: str) -> str | None:
    """Queue-only when the catalogue is curated; None keeps the automatic plan."""
    delivery = store.get_delivery(secret)
    if delivery is None or delivery.auto_select:
        return None
    return "queue"


async def _build(request: Request, secret: str, day: str):
    return await _service(request).build(
        secret=secret,
        reader=await _reader(request, secret),
        local_day=day,
        only=_issue_only(_store(request), secret),
    )


# One running compose per catalogue. Single-process by design (see the Dockerfile
# CMD), the same assumption the scheduler makes. A strong reference is kept
# because the event loop only holds weak ones to running tasks.
_composing: dict[str, asyncio.Task] = {}


async def _compose(app, secret: str):
    """Build and deliver one issue. Returns the Issue, or None if nothing new."""
    delivery = app.state.matins.get_delivery(secret)
    day = local_day(delivery.timezone if delivery else config.get_default_timezone())
    reader = await app.state.tenant_reader(secret)
    issue = await app.state.issues.build(
        secret=secret,
        reader=reader,
        local_day=day,
        only=_issue_only(app.state.matins, secret),
    )
    if issue is not None:
        await app.state.issues.deliver(secret, issue)
    return issue


def _composed(secret: str):
    """Where to go once a compose has finished."""
    task = _composing.pop(secret)
    try:
        issue = task.result()
    except Exception:
        logger.exception("compose failed")
        return _back(
            secret, to="issues/preview", error="Composing failed. Nothing was published; try again."
        )
    if issue is None:
        return _back(
            secret,
            to="issues/preview",
            notice="Nothing new to compose: no unprinted feed items and an empty queue.",
        )
    return _back(
        secret,
        to=f"issues/{issue.id}",
        notice=f"Composed, with {_count(issue.article_count, 'article')}.",
    )


def has_content(request: Request, secret: str) -> bool:
    """Past onboarding: this catalogue has something to read."""
    store = _store(request)
    return bool(store.list_feeds(secret) or _service(request).issues_for(secret))


async def dashboard(
    request: Request,
    secret: str,
    *,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """HTML for `/{secret}/` when a browser asks for it. Called from main's
    tenant root, which owns the route and the cookie."""
    store = _store(request)
    delivery = store.get_delivery(secret)
    return pages.dashboard_page(
        secret=secret,
        base_url=app_config.get_base_url(),
        csrf=_csrf_token(secret),
        feeds=_feed_rows(request, secret),
        saved_count=len(store.list_saved(secret)),
        issues=[_issue_row(issue) for issue in _service(request).issues_for(secret)[:1]],
        schedule_label=_schedule_label(request, secret) or "",
        timezone=_timezone(request, secret),
        scheduling=bool(delivery.enabled) if delivery else False,
        plan=await _plan_summary(request, secret),
        account_email=store.email_for_secret(secret),
        notice=notice,
        error=error,
    )


async def _plan_summary(request: Request, secret: str) -> dict | None:
    reader = await _reader(request, secret)
    try:
        candidates = await asyncio.wait_for(
            _service(request).plan(secret, reader), HOME_PLAN_TIMEOUT
        )
    except TimeoutError:
        return None
    return {"count": len(candidates)}


def _timezone(request: Request, secret: str) -> str:
    delivery = _store(request).get_delivery(secret)
    return delivery.timezone if delivery else config.get_default_timezone()


def _schedule_label(request: Request, secret: str) -> str | None:
    """When the next automatic issue is due, or None if there will not be one."""
    store = _store(request)
    delivery = store.get_delivery(secret)
    if delivery is None or not delivery.enabled:
        return None
    return next_run(
        delivery.schedule,
        delivery.timezone,
        issued_today=store.has_issue_for_day(secret, local_day(delivery.timezone)),
    )


def _feed_row(feed) -> dict:
    return {
        "id": feed.id,
        "title": feed.title or feed.url,
        "url": feed.url,
        "paused": feed.paused,
        "error": feed.last_error,
        "fetched_at": feed.last_fetched_at,
        "item_count": feed.item_count,
        "last_item_at": feed.last_item_at,
        "auth_cookie_set": bool(feed.auth_cookie),
        "voice": feed.voice,
    }


def _account_voice(delivery) -> str:
    return (delivery.voice if delivery else None) or voices.DEFAULT_VOICE


def _feed_rows(request: Request, secret: str) -> list[dict]:
    return [_feed_row(feed) for feed in _store(request).list_feeds(secret)]


def _issue_row(issue) -> dict:
    return {
        "id": issue.id,
        "title": issue.title,
        "count": issue.article_count,
        "words": issue.word_count,
        "created_at": issue.created_at,
    }


def _article_state(
    key: str,
    article,
    now: datetime,
    limits: IssueLimits,
    printed: set[str],
    skipped: set[str],
    planned: set[str],
) -> str:
    if key in printed:
        return "printed"
    if key in skipped:
        return "skipped"
    if key in planned:
        return "next"
    if not _is_fresh(article, now, limits.window_hours):
        return "old"
    return "waiting"


def _is_fresh(article, now: datetime, hours: int) -> bool:
    from .issues import _fresh

    return _fresh(article, now, hours)


def _count(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _wants_html(request: Request) -> bool:
    from ..main import _wants_html as wants

    return wants(request)


def _title_from_url(url: str) -> str:
    from .queue import title_from_url

    return title_from_url(url)


def _back(
    secret: str, *, to: str = "", notice: str | None = None, error: str | None = None
):
    """Post/redirect/get with a one-line message, by default back to home."""
    query = ""
    if notice:
        query = f"?notice={quote(notice)}"
    elif error:
        query = f"?error={quote(error)}"
    return RedirectResponse(f"/{secret}/{to}{query}", status_code=303)


def _html(
    body: str, *, status: int = 200, csp: str | None = None, isolated: bool = False
) -> HTMLResponse:
    response = HTMLResponse(body, status_code=status)
    if csp:
        # The security middleware uses setdefault, so a route that needs a
        # different policy sets it here and wins.
        response.headers["Content-Security-Policy"] = csp
    if isolated:
        # Cross-origin isolation lets the Listen worker run WASM on several
        # threads; single-threaded, Kokoro is slower than speech on a machine
        # without WebGPU. "credentialless" rather than "require-corp", so the
        # article's remote images still load (without cookies). Browsers that
        # do not know it (Safari) stay single-threaded and work as before.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response


def _relative_opds_client(request) -> bool:
    """CrossPoint/INX concatenate hrefs and need bare relative links."""
    ua = request.headers.get("user-agent", "").casefold()
    return "crosspoint" in ua or "xteink" in ua or ua.startswith("inx")


def _feed_response(request, secret, articles, *, title, feed_id, epub_base):
    return Response(
        opds.article_feed(
            f"urn:later-ink:u:{_hash(secret)}:{feed_id}",
            title,
            articles,
            self_href=f"/{secret}/{epub_base}/",
            epub_base=f"/{secret}/{epub_base}",
            start_href=f"/{secret}/",
            origin=_origin(request),
            relative_acquisition=_relative_opds_client(request),
        ),
        media_type=opds.ACQ_TYPE,
    )


def _hash(secret: str) -> str:
    return hashlib.sha1(secret.encode()).hexdigest()[:12]


def _issue_summary(issue) -> str:
    minutes = max(1, round(issue.word_count / 250))
    return f"{issue.article_count} articles · about {minutes} min"


def _render_settings(
    request, secret, *, notice=None, error=None, overrides: dict | None = None
):
    store = _store(request)
    delivery = store.get_delivery(secret)
    values = {
        "email": store.email_for_secret(secret) or (delivery.email if delivery else None) or "",
        "webhook_url": (delivery.webhook_url if delivery else None) or "",
        "schedule": delivery.schedule if delivery else config.get_default_schedule(),
        "timezone": delivery.timezone if delivery else config.get_default_timezone(),
        "enabled": bool(delivery.enabled) if delivery else True,
        "auto_select": delivery.auto_select if delivery else True,
        "show_dates": delivery.show_dates if delivery else True,
        "voice": _account_voice(delivery),
        "limits": {
            key: getattr(delivery, key, None) if delivery else None for key in LIMIT_BOUNDS
        },
    }
    values.update(overrides or {})
    defaults = limits_for(None)
    return pages.settings_page(
        secret=secret,
        base_url=app_config.get_base_url(),
        csrf=_csrf_token(secret),
        email=values["email"],
        webhook_url=values["webhook_url"],
        schedule=values["schedule"],
        timezone=values["timezone"],
        enabled=values["enabled"],
        auto_select=values["auto_select"],
        show_dates=values["show_dates"],
        voice=values["voice"],
        listening=config.get_tts_assets_dir() is not None,
        webhook_secret_set=bool(delivery and delivery.webhook_secret),
        limits=values["limits"],
        defaults={key: getattr(defaults, key) for key in LIMIT_BOUNDS},
        bounds=LIMIT_BOUNDS,
        schedule_label=_schedule_label(request, secret),
        notice=notice,
        error=error,
    )


def _parse_limit(key: str, raw: str) -> int | None:
    """A limit from the settings form; blank means the instance default."""
    value = (raw or "").strip()
    if not value:
        return None
    low, high = LIMIT_BOUNDS[key]
    label = key.replace("_", " ")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{label.capitalize()} must be a whole number.") from None
    if not low <= number <= high:
        raise ValueError(f"{label.capitalize()} must be between {low} and {high}.")
    return number


def _secrets_unchanged(previous, submitted: str) -> str | None:
    """Resolve the webhook secret field.

    Blank keeps the existing one, so the form never has to echo a signing key
    back into the page (where it would sit in browser history). A lone "-"
    clears it.
    """
    submitted = submitted.strip()
    if submitted == "-":
        return None
    if submitted:
        return submitted
    return previous.webhook_secret if previous else None


def _normalize_schedule(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    hour, _, minute = value.partition(":")
    try:
        h, m = int(hour), int(minute or 0)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return f"{h:02d}:{m:02d}"


def _valid_timezone(name: str) -> bool:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True
