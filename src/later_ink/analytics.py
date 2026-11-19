"""Server-side page counting, posted to a nilleb-analytics compatible collector.

The pages carry no JavaScript beyond one hashed inline script, so a tracking
snippet would mean loosening the CSP. Counting on the server instead also sees
what a snippet never could: the OPDS fetches and EPUB downloads of e-readers.

Off unless ANALYTICS_COLLECT_URL is set. What leaves the box:
- the route *template* (`/{secret}/read/{article_id:path}`), never the request
  path — the catalogue secret is the credential, and it sits in the path;
- the referrer, only when it is another site, and without its query string;
- the visitor's IP, User-Agent and Accept-Language as headers. The collector
  hashes them with a salt it replaces daily into a per-day visitor id and does
  not store the IP. Its reverse proxy has to trust this host's X-Forwarded-For,
  or every visitor shows up with this server's address.
"""

import asyncio
import logging
from urllib.parse import urlsplit

import httpx

from . import config

logger = logging.getLogger(__name__)

# The kinds of response worth counting, by content type. JSON (/api/v1, the
# reader's fetches), audio and the OpenSearch descriptions are machinery.
_EVENT_TYPES = {
    "text/html": "page_view",
    "application/atom+xml": "opds",
    "application/epub+zip": "download",
}

_SKIPPED_PREFIXES = ("/assets/", "/api/", "/admin/")
_SKIPPED_PATHS = frozenset({"/health", "/healthz", "/version"})

_TIMEOUT = httpx.Timeout(3.0)

# Fire-and-forget tasks must be referenced until done, or the loop may drop them.
_pending: set[asyncio.Task] = set()


def event_type(method: str, path: str, status: int, content_type: str) -> str | None:
    """What to count this response as, or None to leave it out."""
    if method != "GET" or status >= 400:
        return None
    if path in _SKIPPED_PATHS or path.startswith(_SKIPPED_PREFIXES):
        return None
    return _EVENT_TYPES.get(content_type.split(";")[0].strip().lower())


def external_referrer(referer: str | None, own_host: str) -> str | None:
    """The referring page as scheme://host/path, unless it is this site."""
    if not referer:
        return None
    parts = urlsplit(referer)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.hostname == own_host or parts.hostname == f"www.{own_host}":
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


async def _post(url: str, payload: dict, headers: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning("analytics: collector answered %s", resp.status_code)
    except httpx.HTTPError as e:
        logger.warning("analytics: %s", e.__class__.__name__)


def track(
    *,
    route_path: str,
    kind: str,
    referer: str | None,
    client_ip: str,
    user_agent: str | None,
    accept_language: str | None,
) -> None:
    """Queue one event; returns at once and never raises into the request."""
    url = config.get_analytics_collect_url()
    if not url:
        return
    base_url = config.get_base_url()
    payload = {
        "page_url": f"{base_url}{route_path}",
        "referrer": external_referrer(referer, urlsplit(base_url).hostname or ""),
        "event_type": kind,
    }
    headers = {"X-Forwarded-For": client_ip}
    if user_agent:
        headers["User-Agent"] = user_agent
    if accept_language:
        headers["Accept-Language"] = accept_language
    task = asyncio.get_running_loop().create_task(_post(url, payload, headers))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
