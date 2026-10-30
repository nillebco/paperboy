"""JSON API for agents and scripts.

The catalogue secret is the credential, presented as a bearer token so it does
not end up in access logs the way the OPDS paths do. Everything here is the same
service the pages call: nothing in this module decides what an issue contains.

Deliberate omissions: no endpoint writes a composed issue's contents, and none
returns article bodies. An agent's job is to put things in and ask what is
waiting — reading happens on the device, which is the product.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..connectors.base import UpstreamError
from . import config, routes, rss
from .issues import local_day

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

MAX_CONTENT_BYTES = 512 * 1024


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


# -------------------------------------------------------------------- items


@router.get("/items")
async def list_items(request: Request, status: str = "unread", limit: int = 50):
    """The queue, and what the reader has already seen.

    `status` is `unread`, `read` or `all`. Only the saved queue is listed:
    feed items are fetched from their source and are not ours to enumerate
    cheaply, so an agent that wants those should ask for the issue preview.
    """
    if status not in ("unread", "read", "all"):
        raise HTTPException(400, "status must be unread, read or all")
    secret = _secret(request)
    store = _store(request)
    saved = store.list_saved(secret, include_consumed=status != "unread")
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
    queued = _store(request).save_url(secret, url, title, content=content)
    return JSONResponse(
        {"queued": queued, "url": url}, status_code=201 if queued else 200
    )


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
async def preview_issue(request: Request):
    """Exactly what the next issue would contain, without composing it."""
    secret = _secret(request)
    reader = await request.app.state.tenant_reader(secret)
    candidates = await request.app.state.issues.plan(secret, reader)
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
async def compose_issue(request: Request, force: int = 0):
    """Compose and deliver now. Idempotent per local day unless `force=1`."""
    secret = _secret(request)
    store = _store(request)
    service = request.app.state.issues
    delivery = store.get_delivery(secret)
    day = local_day(delivery.timezone if delivery else config.get_default_timezone())
    if not force and store.has_issue_for_day(secret, day):
        raise HTTPException(409, "An issue already exists for today; pass force=1 to build another")
    reader = await request.app.state.tenant_reader(secret)
    issue = await service.build(secret=secret, reader=reader, local_day=day)
    if issue is None:
        return JSONResponse({"issue": None, "reason": "nothing new to compose"}, status_code=200)
    await service.deliver(secret, issue)
    return {
        "id": issue.id,
        "title": issue.title,
        "articles": issue.article_count,
        "words": issue.word_count,
        "download": f"/{secret}/issues/{issue.id}.epub",
    }


# ------------------------------------------------------------------- shared


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
