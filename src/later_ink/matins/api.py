"""JSON API for agents and scripts.

The catalogue secret is the credential, presented as a bearer token so it does
not end up in access logs the way the OPDS paths do. Everything here is the same
service the pages call: nothing in this module decides what an issue contains.

Deliberate omissions: no endpoint writes a composed issue's contents, and only
`/items/long` returns article text, so an agent can write a recap. An agent's job
is to put things in and ask what is waiting — reading happens on the device,
which is the product.
"""

import hashlib
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..connectors.base import ArticleUnavailable, UpstreamError
from . import config, extract, routes, rss, taste
from .issues import limits_for, local_day

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

MAX_CONTENT_BYTES = 512 * 1024
MAX_LEDE_CHARS = 2000
MAX_RECAP_CHARS = 2000
MAX_RECAP_SOURCE_WORDS = 15000


# --------------------------------------------------------------------- auth


def _secret(request: Request) -> str:
    """Read the catalogue secret from the Authorization header.

    Bearer is the documented form; X-Matins-Secret exists because some tool
    builders only expose custom headers. A secret in the query string is
    deliberately not accepted here — that is what the browser routes are for,
    and it would end up in every proxy log.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    else:
        supplied = request.headers.get("x-matins-secret", "").strip()
    if not supplied:
        raise HTTPException(
            401,
            "Missing credentials. Send Authorization: Bearer <catalogue secret>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    from ..main import _resolve_secret

    _resolve_secret(supplied, request)
    return supplied


def _store(request: Request):
    return request.app.state.matins


# -------------------------------------------------------------------- feeds


@router.get("/feeds")
async def list_feeds(request: Request):
    secret = _secret(request)
    return {
        "feeds": [
            {"id": feed.id, "url": feed.url, "title": feed.title}
            for feed in _store(request).list_feeds(secret)
        ]
    }


@router.post("/feeds", status_code=201)
async def add_feed(request: Request):
    secret = _secret(request)
    body = await _json(request)
    url = _url(body.get("url"))
    store = _store(request)
    if any(feed.url == url for feed in store.list_feeds(secret)):
        return JSONResponse({"url": url, "created": False}, status_code=200)
    try:
        url, parsed = await rss.resolve_feed(url)
    except UpstreamError as e:
        raise HTTPException(422, str(e)) from None
    if any(feed.url == url for feed in store.list_feeds(secret)):
        return JSONResponse({"url": url, "created": False}, status_code=200)
    feed = routes.subscribe(request, secret, url, parsed)
    return {"id": feed.id, "url": url, "title": parsed.title, "created": True}


@router.delete("/feeds/{feed_id}", status_code=204)
async def delete_feed(feed_id: str, request: Request):
    secret = _secret(request)
    if not _store(request).delete_feed(secret, feed_id):
        raise HTTPException(404, "No such feed")
    return None


# ---------------------------------------------------------------- candidates


@router.post("/candidates", status_code=201)
async def add_candidates(request: Request):
    """Offer items the reader should show but the composer must not print.

    Bluesky is the first user: a post cannot be fetched by URL, so the curator
    sends its text. The item becomes printable only when queued (with that text).
    """
    secret = _secret(request)
    body = await _json(request)
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(400, "items must be a non-empty list")
    if len(raw_items) > 200:
        raise HTTPException(400, "at most 200 candidates per request")

    cleaned = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise HTTPException(400, "every candidate must be an object")
        url = _url(raw.get("url"))
        content = raw.get("content")
        if content is not None and not isinstance(content, str):
            raise HTTPException(400, "content must be a string")
        if content is not None and len(content.encode()) > MAX_CONTENT_BYTES:
            raise HTTPException(413, f"content is limited to {MAX_CONTENT_BYTES} bytes")
        published = raw.get("published")
        if not isinstance(published, (int, float)) or isinstance(published, bool):
            published = None
        cleaned.append(
            {
                "id": str(raw.get("id") or "").strip()
                or hashlib.sha1(url.encode()).hexdigest()[:16],
                "url": url,
                "title": str(raw.get("title") or "")[:300],
                "source": str(raw.get("source") or "")[:120],
                "summary": str(raw.get("summary") or "")[:600],
                "content": content,
                "published": float(published) if published is not None else None,
            }
        )
    added = _store(request).upsert_candidates(secret, cleaned)
    return {"added": added}


@router.get("/candidates")
async def list_candidates(request: Request, limit: int = 100):
    secret = _secret(request)
    return {
        "candidates": [
            {
                "id": item.id,
                "url": item.url,
                "title": item.title,
                "source": item.source,
                "published": item.published,
            }
            for item in _store(request).list_candidates(
                secret, limit=max(1, min(limit, 500))
            )
        ]
    }


# --------------------------------------------------------------------- score


_MAX_SCORE_ITEMS = 500


@router.post("/score")
async def score_items(request: Request):
    """How close each item is to what this catalogue's reader liked and read.

    For an external curator ranking articles Matins has not seen. `scores` is
    empty when there is no taste yet (too few signals, or no model): rank as if
    this endpoint did not exist."""
    secret = _secret(request)
    items = (await _json(request)).get("items")
    if not isinstance(items, list) or len(items) > _MAX_SCORE_ITEMS:
        raise HTTPException(400, f"Expected items: a list of at most {_MAX_SCORE_ITEMS}")
    profile = taste.taste_for(_store(request), secret)
    if profile is None:
        return {"scores": {}, "signals": 0}
    scores = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            continue
        text = taste.article_text(str(item["title"])[:1000], str(item.get("summary") or "")[:400])
        value = profile.score(text)
        if value is not None:
            scores[str(item["id"])[:200]] = round(value, 4)
    return {"scores": scores, "signals": profile.signals}


# ------------------------------------------------------------------- signals


@router.get("/signals")
async def list_signals(request: Request, limit: int = 500):
    """Everything the reader expressed, explicit and implicit.

    An explicit like/dislike wins. Otherwise opening an article counts for less
    than handing it to the publisher, and both count for less than a like. The
    `weight` is what a curator should aggregate; `rating` stays for clients that
    only know up/down.
    """
    ordered = taste.weighted_signals(_store(request), _secret(request))
    return {"signals": ordered[: max(1, min(limit, 2000))]}


@router.post("/signals", status_code=201)
async def set_signal(request: Request):
    secret = _secret(request)
    body = await _json(request)
    url = _url(body.get("url"))
    rating = _rating(body.get("rating"))
    store = _store(request)
    if rating == 0:
        store.clear_signal(secret, url)
    else:
        store.set_signal(
            secret,
            url,
            rating,
            title=str(body.get("title") or "")[:300],
            source=str(body.get("source") or "")[:120],
        )
    return {"url": url, "rating": _rating_name(rating)}


# -------------------------------------------------------------------- items


@router.get("/items")
async def list_items(request: Request, status: str = "unread", limit: int = 50):
    """The queue, and what the reader has already seen.

    `status` is `unread`, `read`, `queued` or `all`. `queued` is what will still
    print, read or not. Only the saved queue is listed:
    feed items are fetched from their source and are not ours to enumerate
    cheaply, so an agent that wants those should ask for the issue preview.
    """
    if status not in ("unread", "read", "queued", "all"):
        raise HTTPException(400, "status must be unread, read, queued or all")
    secret = _secret(request)
    store = _store(request)
    saved = store.list_saved(secret, include_consumed=status in ("read", "all"))
    ids = [f"saved:{item.id}" for item in saved]
    read = store.read_ids(secret, ids)
    rows = []
    for item in saved:
        is_read = f"saved:{item.id}" in read or item.consumed_by is not None
        if status == "unread" and is_read:
            continue
        if status == "read" and not is_read:
            continue
        rows.append(
            {
                "id": item.id,
                "url": item.url,
                "title": item.title,
                "added_at": int(item.added_at),
                "read": is_read,
                "has_content": bool(item.content),
                "origin": item.origin,
            }
        )
    return {"items": rows[: max(1, min(limit, 200))]}


@router.post("/items", status_code=201)
async def add_item(request: Request):
    """Queue a link, optionally with the article body.

    Supplying `content` is what makes this usable from an agent that has already
    fetched and cleaned a page — and the only way to queue something the server
    cannot fetch itself (a paywall, an authenticated page, generated text).
    """
    secret = _secret(request)
    body = await _json(request)
    url = _url(body.get("url"))
    title = body.get("title")
    title = str(title)[:300] if title else None
    content = body.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise HTTPException(400, "content must be a string of HTML or text")
        if len(content.encode()) > MAX_CONTENT_BYTES:
            raise HTTPException(413, f"content is limited to {MAX_CONTENT_BYTES} bytes")
        content = content.strip() or None
    origin = body.get("origin", "user")
    if origin not in ("user", "curator"):
        raise HTTPException(400, "origin must be user or curator")
    lede = body.get("lede")
    lede = str(lede)[:MAX_LEDE_CHARS] if lede else None
    published = body.get("published")
    if published is not None and (isinstance(published, bool) or not isinstance(published, int | float)):
        raise HTTPException(400, "published must be epoch seconds")
    queued = _store(request).save_url(
        secret, url, title, content=content, origin=origin, lede=lede, published=published
    )
    return JSONResponse(
        {"queued": queued, "url": url}, status_code=201 if queued else 200
    )


@router.get("/items/long")
async def long_items(request: Request):
    """Queued articles the next issue would print that are long enough to need a
    recap and have none. An agent writes them, then PUTs them back."""
    secret = _secret(request)
    store = _store(request)
    reader = await routes._reader(request, secret)
    queue = reader.sources["later"]
    articles, _ = await queue.list_articles("inbox")
    recapped = {item.id for item in store.list_saved(secret) if item.recap}
    threshold = config.get_recap_min_words()
    rows = []
    for article in articles[: limits_for(store.get_delivery(secret)).max_saved]:
        if int(article.id) in recapped:
            continue
        try:
            _, html = await queue.get_article_html(article.id)
        except (ArticleUnavailable, UpstreamError):
            continue
        text = extract.visible_text(html)
        words = text.split()
        if len(words) >= threshold:
            rows.append(
                {
                    "id": int(article.id),
                    "url": article.url,
                    "title": article.title,
                    "words": len(words),
                    "text": " ".join(words[:MAX_RECAP_SOURCE_WORDS]),
                }
            )
    return {"items": rows, "min_words": threshold}


@router.put("/items/{item_id}/recap", status_code=204)
async def set_recap(item_id: int, request: Request):
    body = await _json(request)
    recap = body.get("recap")
    if not isinstance(recap, str) or not recap.strip():
        raise HTTPException(400, "recap must be text")
    if len(recap) > MAX_RECAP_CHARS:
        raise HTTPException(413, f"recap is limited to {MAX_RECAP_CHARS} characters")
    if not _store(request).set_saved_recap(_secret(request), item_id, recap.strip()):
        raise HTTPException(404, "No such item")


@router.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int, request: Request):
    secret = _secret(request)
    if not _store(request).delete_saved(secret, item_id):
        raise HTTPException(404, "No such item")
    return None


# ------------------------------------------------------------------- issues


@router.get("/issues")
async def list_issues(request: Request):
    secret = _secret(request)
    return {
        "issues": [
            {
                "id": issue.id,
                "title": issue.title,
                "created_at": int(issue.created_at),
                "articles": issue.article_count,
                "words": issue.word_count,
                "download": f"/{secret}/issues/{issue.id}.epub",
            }
            for issue in request.app.state.issues.issues_for(secret)
        ]
    }


@router.get("/issues/preview")
async def preview_issue(request: Request, only: str | None = None):
    """Exactly what the next issue would contain, without composing it.

    `only=queue` previews a curated issue: the saved queue alone, without the
    automatic feed selection.
    """
    only = _only(only)
    secret = _secret(request)
    reader = await request.app.state.tenant_reader(secret)
    candidates = await request.app.state.issues.plan(secret, reader, only=only)
    return {
        "articles": [
            {
                "title": candidate.article.title,
                "section": candidate.section,
                "source": candidate.source,
                "url": candidate.article.url,
                "estimated_words": candidate.estimated_words,
            }
            for candidate in candidates
        ]
    }


@router.post("/issues", status_code=201)
async def compose_issue(request: Request, force: int = 0, only: str | None = None):
    """Compose and deliver now. Idempotent per local day unless `force=1`.

    `only=queue` composes from the queue alone — the entry point for an agent
    that curates the day's articles itself and does not want the automatic feed
    selection added on top.
    """
    only = _only(only)
    secret = _secret(request)
    store = _store(request)
    service = request.app.state.issues
    delivery = store.get_delivery(secret)
    day = local_day(delivery.timezone if delivery else config.get_default_timezone())
    if not force and store.has_issue_for_day(secret, day):
        raise HTTPException(409, "An issue already exists for today; pass force=1 to build another")
    reader = await request.app.state.tenant_reader(secret)
    # Explicit here (build repeats it) so the caller learns what was dropped.
    expired = service.expire_queue(secret)
    skipped: list[dict] = []
    issue = await service.build(
        secret=secret, reader=reader, local_day=day, only=only, skipped=skipped
    )
    if issue is None:
        return JSONResponse(
            {
                "issue": None,
                "reason": "nothing new to compose",
                "expired": expired,
                "skipped": skipped,
            },
            status_code=200,
        )
    await service.deliver(secret, issue)
    return {
        "id": issue.id,
        "title": issue.title,
        "articles": issue.article_count,
        "words": issue.word_count,
        "download": f"/{secret}/issues/{issue.id}.epub",
        "expired": expired,
        "skipped": skipped,
    }


@router.get("/archived")
async def list_archived(request: Request, hours: float = 72, limit: int = 5000):
    """URLs archived in the last `hours`: printed, or dismissed in the reader.

    A curator excludes them from its shortlist; the window only needs to cover
    the age of what it considers."""
    secret = _secret(request)
    since = time.time() - max(1.0, min(hours, 24 * 365)) * 3600
    return {"urls": _store(request).archived_since(secret, since, limit=limit)}


# ------------------------------------------------------------------- shared


def _only(value: str | None) -> str | None:
    """Validate the optional source filter. Only the queue is selectable today:
    "everything" is the absence of the parameter, which keeps the common call
    and every existing client unchanged."""
    if value in (None, ""):
        return None
    if value != "queue":
        raise HTTPException(400, "only must be 'queue' when given")
    return value


def _rating(value) -> int:
    if isinstance(value, bool):
        return 1 if value else -1
    if isinstance(value, (int, float)):
        return 1 if value > 0 else -1 if value < 0 else 0
    text = str(value or "").strip().casefold()
    if text in ("interesting", "like", "liked", "up", "yes", "more"):
        return 1
    if text in ("not_interested", "not-interested", "dislike", "down", "no", "less"):
        return -1
    return 0


def _rating_name(rating: int) -> str:
    if rating > 0:
        return "interesting"
    if rating < 0:
        return "not_interested"
    return "none"


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


def _url(raw) -> str:
    url = routes._valid_url(str(raw or ""))
    if url is None:
        raise HTTPException(400, "Expected an http(s) url")
    return url
