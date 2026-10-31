"""Signed webhook delivery: the signal that an issue is ready.

Delivery is a notification, never a content channel. The issue stays on the
server and the reader pulls it over OPDS; the webhook exists so something else
(a phone bridge, Home Assistant, a ntfy topic, a wake-the-device script) can
react without polling.

Two properties matter more than the request shape:

- **It is verifiable.** HMAC over `timestamp.body`, so a receiver can tell a
  real delivery from anything else that can reach its endpoint, and can bound
  replay with the timestamp.
- **It cannot be aimed inward.** The URL is user-supplied on a multi-tenant
  instance, which is the textbook SSRF setup: `http://169.254.169.254/...` or an
  internal admin port. Redirects are not followed, and the same address guard
  the image fetcher uses applies here, unless a self-hoster opts in.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx

from ..fetch import _validate
from . import config

logger = logging.getLogger(__name__)

ISSUE_READY = "issue.ready"
SUPPORTED_EVENTS = (ISSUE_READY,)

# Stripe's header shape, because every receiver library already knows how to
# parse it: `t=<unix>,v1=<hex>`. The version tag leaves room for a key rotation
# to accept both for a window.
SIGNATURE_HEADER = "X-Matins-Signature"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={sign(secret, timestamp, body)}"


def verify(header: str, secret: str, body: bytes, *, tolerance: int = 300) -> bool:
    """Receiver-side check, shipped so tests and examples use the real thing.

    Tolerates a clock skew of `tolerance` seconds in both directions: a sender
    whose clock is a little ahead is common, and rejecting it would look like a
    broken webhook rather than a replay guard.
    """
    parts = dict(
        part.split("=", 1) for part in header.split(",") if "=" in part
    )
    try:
        timestamp = int(parts.get("t", ""))
        provided = parts.get("v1", "")
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), provided)


async def deliver(
    url: str,
    secret: str | None,
    payload: dict,
    *,
    allow_private: bool | None = None,
) -> bool:
    """POST the payload, retrying transient failures. True on a 2xx.

    A missing secret still delivers, unsigned. That is a legitimate setup for a
    receiver on a trusted network, and refusing it would be worse than saying
    plainly in the docs that an unsigned hook is only as private as its URL.
    """
    if allow_private is None:
        allow_private = config.webhook_allow_private()

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    attempts = config.get_webhook_attempts()
    timeout = config.get_webhook_timeout()

    for attempt in range(attempts):
        if not allow_private:
            try:
                await _validate(url)
            except ValueError as e:
                logger.warning("refusing webhook target %s: %s", _redact(url), e)
                return False

        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Matins-Webhook/1",
            "X-Matins-Event": str(payload.get("event", "")),
        }
        if secret:
            headers[SIGNATURE_HEADER] = signature_header(secret, timestamp, body)

        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.post(url, content=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            logger.info("webhook attempt %d/%d failed: %s", attempt + 1, attempts, e)
        else:
            if 200 <= response.status_code < 300:
                return True
            # 4xx other than 429 is the receiver saying "never send this again";
            # retrying only delays the log line that says so.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                logger.warning(
                    "webhook rejected with %s; not retrying", response.status_code
                )
                return False
            logger.info(
                "webhook attempt %d/%d got %s",
                attempt + 1,
                attempts,
                response.status_code,
            )

        if attempt + 1 < attempts:
            await asyncio.sleep(2**attempt)

    return False


def issue_payload(issue, catalog_url: str) -> dict:
    base = catalog_url.rstrip("/")
    issue_url = f"{base}/{issue.id}"
    return {
        "event": ISSUE_READY,
        "delivered_at": int(time.time()),
        "issue": {
            "id": issue.id,
            "title": issue.title,
            "article_count": issue.article_count,
            "word_count": issue.word_count,
            "created_at": int(issue.created_at),
            "download_url": f"{issue_url}.epub",
            "web_url": issue_url,
        },
        "issue_url": issue_url,
        "catalog_url": catalog_url,
    }


def _redact(url: str) -> str:
    """Log the shape of a rejected target without its credentials or query."""
    try:
        parts = httpx.URL(url)
    except (TypeError, ValueError):
        return "(unparseable url)"
    host = parts.host or ""
    return f"{parts.scheme}://{host}:{parts.port}" if parts.port else f"{parts.scheme}://{host}"
