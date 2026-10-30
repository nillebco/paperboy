"""Matins HTTP surface: catalogue dashboard, sources, reader, issues, admin.

Mounted before the tenant routes in `later_ink.main`, which is what makes
`/{secret}/issues/` and `/{secret}/read` reach these handlers instead of the
generic folder route.

Auth is the catalogue secret itself, the same bearer-in-the-URL model the rest of
the app uses. Every state-changing form additionally carries a CSRF token derived
from the app key, because a browser is the only thing that submits them.
"""

import hashlib
import hmac
import logging
import secrets as pysecrets
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import config as app_config
from .. import opds
from ..connectors.base import Article, ArticleUnavailable, UpstreamError
from . import catalogues, config, notify, pages, rss
from .issues import IssueService, local_day
from .reader import to_page_html

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FEEDS = 100
MAX_URL_LENGTH = 2048
MAX_READER_ITEMS = 120
EPUB_MEDIA = "application/epub+zip"

# Sign-in codes: six digits, ten minutes, five tries, one send a minute.
OTP_TTL = 600.0
OTP_MAX_ATTEMPTS = 5

# Article pages are the one place that may load remote images: proxying them
# would mean fetching third-party content on the user's behalf and caching it,
# which is the EPUB builder's job, not the browser's. Links still cannot run
# anything, and there is no script of our own on the page.
READER_CSP = (
    "default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'self'"
)


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


@router.post("/{secret}/feeds")
async def add_feed(
    secret: str, request: Request, csrf: str = Form(""), url: str = Form("")
):
    """Add one subscription, validating it now rather than at 07:00 tomorrow.

    The URL is fetched and parsed once here so a typo, a 404 or an HTML page
    pasted as a feed produces an immediate, specific message instead of a
    silently empty issue.
    """
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    target = _valid_url(url)
    if not target:
        return _back(secret, error="That is not an http(s) URL.")

    store = _store(request)
    if any(feed.url == target for feed in store.list_feeds(secret)):
        return _back(secret, notice="That feed is already subscribed.")
    if len(store.list_feeds(secret)) >= MAX_FEEDS:
        return _back(secret, error=f"The limit is {MAX_FEEDS} feeds.")

    try:
        body = await rss.fetch_feed(
            target,
            timeout=config.get_feed_fetch_timeout(),
            max_bytes=config.get_feed_max_bytes(),
        )
        parsed = rss.parse_feed(body, target)
    except UpstreamError as e:
        return _back(secret, error=f"Could not use that feed: {e}")
    except Exception:
        logger.exception("feed add failed")
        return _back(secret, error="Could not read that feed.")

    store.add_feed(secret, target, parsed.title[:120])
    return _back(secret, notice=f"Added {parsed.title or target}.")


@router.post("/{secret}/feeds/{feed_id}/delete")
async def remove_feed(secret: str, feed_id: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not _store(request).delete_feed(secret, feed_id):
        raise HTTPException(404, "No such feed")
    return _back(secret, notice="Feed removed.")


@router.post("/{secret}/saved/{saved_id}/delete")
async def remove_saved(secret: str, saved_id: int, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    if not _store(request).delete_saved(secret, saved_id):
        raise HTTPException(404, "No such saved link")
    return _back(secret, notice="Removed from the queue.")


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

    url = _valid_url(url)
    if not url:
        raise HTTPException(400, "Expected an http(s) url")
    queued = _store(request).save_url(secret, url, title)
    if request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(
            {"queued": queued, "url": url}, status_code=201 if queued else 200
        )
    return _back(secret, notice="Saved for the next issue." if queued else "Already saved.")


# ------------------------------------------------------------------- reader


@router.get("/{secret}/read", response_class=HTMLResponse)
async def reader_index(secret: str, request: Request, all: int = 0):
    """The unread list: everything the sources currently publish, newest first.

    This is the part the OPDS catalogue cannot do — an e-reader browse is
    folder-at-a-time, and "what is new across everything" is a question only a
    server can answer cheaply."""
    _resolve(secret, request)
    stream = await _reader_items(request, secret)
    read = _store(request).read_ids(secret, [entry[0] for entry in stream])
    items = [
        {
            "id": item_id,
            "title": article.title,
            "source": section,
            "saved": source == "later",
            "minutes": max(1, round((article.word_count or 400) / 250)),
            "read": item_id in read,
        }
        for item_id, article, section, source in stream
    ]
    shown = items if all else [item for item in items if not item["read"]]
    return _html(
        pages.reader_page(
            secret=secret,
            csrf=_csrf_token(secret),
            items=shown[:MAX_READER_ITEMS],
            total=len(items),
            unread=len([item for item in items if not item["read"]]),
            show_all=bool(all),
        )
    )


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
    _store(request).mark_read(secret, article_id)
    return _html(
        pages.reader_article_page(
            secret=secret,
            csrf=_csrf_token(secret),
            article_id=article_id,
            title=article.title,
            author=article.author,
            source_url=article.url,
            body=body,
            minutes=max(1, round((article.word_count or 400) / 250)),
        ),
        csp=READER_CSP,
    )


@router.post("/{secret}/read/{article_id:path}/unread")
async def reader_mark_unread(
    secret: str, article_id: str, request: Request, csrf: str = Form("")
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    _store(request).mark_unread(secret, article_id)
    return RedirectResponse(f"/{secret}/read", status_code=303)


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

    items.sort(key=lambda item: item[1].content_date or datetime.min, reverse=True)
    return items


# --------------------------------------------------------------- settings


@router.get("/{secret}/settings", response_class=HTMLResponse)
async def settings_get(secret: str, request: Request, saved: int = 0):
    _resolve(secret, request)
    return _html(
        _render_settings(request, secret, notice="Settings saved." if saved else None)
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
):
    """Delivery settings only. Sources are managed on the catalogue page."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)

    overrides = {
        "email": email,
        "webhook_url": webhook_url,
        "schedule": schedule,
        "timezone": timezone,
        "enabled": bool(enabled),
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
    return RedirectResponse(f"/{secret}/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ issues


@router.get("/{secret}/issues/preview", response_class=HTMLResponse)
async def issues_preview(secret: str, request: Request):
    """The next issue's selection, without writing anything."""
    _resolve(secret, request)
    reader = await _reader(request, secret)
    candidates = await _service(request).plan(secret, reader)
    return _html(
        pages.preview_page(
            secret=secret,
            csrf=_csrf_token(secret),
            candidates=[
                {
                    "title": c.article.title,
                    "section": c.section,
                    "source": c.source,
                    "words": c.estimated_words,
                }
                for c in candidates
            ],
            window_hours=config.get_issue_window_hours(),
            max_articles=config.get_issue_max_articles(),
        )
    )


@router.post("/{secret}/issues/build", response_class=HTMLResponse)
async def build_now(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    delivery = _store(request).get_delivery(secret)
    day = local_day(delivery.timezone if delivery else config.get_default_timezone())
    issue = await _build(request, secret, day)
    if issue is None:
        message = "Nothing new to compose — no unprinted feed items and an empty queue."
    else:
        message = (
            f"Composed “{issue.title}” with {issue.article_count} articles. "
            "It is in the Issues folder now."
        )
        await _service(request).deliver(secret, issue)
    return _html(pages.notice_page("Matins", message, f"/{secret}/"))


@router.api_route("/{secret}/issues/", methods=["GET", "HEAD"])
async def issues_feed(secret: str, request: Request):
    _resolve(secret, request)
    issues = _service(request).issues_for(secret)
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


async def _build(request: Request, secret: str, day: str):
    return await _service(request).build(
        secret=secret, reader=await _reader(request, secret), local_day=day
    )


def dashboard(
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
    saved = store.list_saved(secret)
    return pages.dashboard_page(
        secret=secret,
        base_url=app_config.get_base_url(),
        csrf=_csrf_token(secret),
        feeds=[
            {"id": feed.id, "title": feed.title or feed.url, "url": feed.url}
            for feed in store.list_feeds(secret)
        ],
        saved=[
            {
                "id": item.id,
                "title": item.title or _title_from_url(item.url),
                "url": item.url,
            }
            for item in saved
        ],
        issues=[
            {
                "id": issue.id,
                "title": issue.title,
                "count": issue.article_count,
                "words": issue.word_count,
            }
            for issue in _service(request).issues_for(secret)[:8]
        ],
        schedule=delivery.schedule if delivery else config.get_default_schedule(),
        timezone=delivery.timezone if delivery else config.get_default_timezone(),
        scheduling=bool(delivery.enabled) if delivery else False,
        notice=notice,
        error=error,
    )


def _title_from_url(url: str) -> str:
    from .queue import title_from_url

    return title_from_url(url)


def _back(secret: str, *, notice: str | None = None, error: str | None = None):
    """Post/redirect/get back to the catalogue with a one-line message."""
    query = ""
    if notice:
        query = f"?notice={quote(notice)}"
    elif error:
        query = f"?error={quote(error)}"
    return RedirectResponse(f"/{secret}/{query}", status_code=303)


def _html(body: str, *, status: int = 200, csp: str | None = None) -> HTMLResponse:
    response = HTMLResponse(body, status_code=status)
    if csp:
        # The security middleware uses setdefault, so a route that needs a
        # different policy sets it here and wins.
        response.headers["Content-Security-Policy"] = csp
    return response


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
    delivery = _store(request).get_delivery(secret)
    values = {
        "email": (delivery.email if delivery else None) or "",
        "webhook_url": (delivery.webhook_url if delivery else None) or "",
        "schedule": delivery.schedule if delivery else config.get_default_schedule(),
        "timezone": delivery.timezone if delivery else config.get_default_timezone(),
        "enabled": bool(delivery.enabled) if delivery else True,
    }
    values.update(overrides or {})
    return pages.settings_page(
        secret=secret,
        csrf=_csrf_token(secret),
        email=values["email"],
        webhook_url=values["webhook_url"],
        schedule=values["schedule"],
        timezone=values["timezone"],
        enabled=values["enabled"],
        webhook_secret_set=bool(delivery and delivery.webhook_secret),
        notice=notice,
        error=error,
    )


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
