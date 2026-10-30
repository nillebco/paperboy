"""Remember which catalogues this browser owns.

The catalogue secret is the only credential, and it lives in the URL. That is
right for an e-reader and terrible for a human: after closing the tab there is
nothing to come back to, and the landing page offers nothing but "create
another one". So a signed, HttpOnly cookie keeps a short list of the secrets
this browser has opened, and the landing page shows "your catalogues".

HttpOnly rather than localStorage on purpose: the value is a credential, and
the one script-bearing page in the app should not be able to read it. Signed
with the app's CSRF key so the list cannot be forged by another site — not
because a forged entry grants access (the secret is still required), but so a
garbage cookie degrades to "no catalogues" instead of being reflected into the
page.
"""

import base64
import hmac
import json
import logging

from fastapi import Request, Response

logger = logging.getLogger(__name__)

COOKIE = "matins_catalogues"
# Enough for a family and a couple of devices; bounded so the header stays
# small and the landing page cannot be made arbitrarily long.
MAX_CATALOGUES = 12
MAX_AGE = 400 * 24 * 3600


def read(request: Request) -> list[str]:
    raw = request.cookies.get(COOKIE, "")
    if not raw or "." not in raw:
        return []
    payload, _, signature = raw.rpartition(".")
    expected = _sign(request, payload)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return []
    try:
        decoded = base64.urlsafe_b64decode(payload.encode()).decode()
        values = json.loads(decoded)
    except (ValueError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str) and 0 < len(v) <= 64][:MAX_CATALOGUES]


def remember(secret: str, request: Request, response: Response) -> None:
    _write(request, response, [secret, *[s for s in read(request) if s != secret]])


def forget(secret: str, request: Request, response: Response) -> None:
    _write(request, response, [s for s in read(request) if s != secret])


def replace(old: str, new: str, request: Request, response: Response) -> None:
    _write(request, response, [new, *[s for s in read(request) if s not in (old, new)]])


def _write(request: Request, response: Response, secrets: list[str]) -> None:
    if not secrets:
        response.delete_cookie(COOKIE, path="/")
        return
    payload = base64.urlsafe_b64encode(
        json.dumps(secrets[:MAX_CATALOGUES], separators=(",", ":")).encode()
    ).decode()
    response.set_cookie(
        COOKIE,
        f"{payload}.{_sign(request, payload)}",
        max_age=MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


def _sign(request: Request, payload: str) -> str:
    key = request.app.state.csrf_key
    return hmac.new(key, f"catalogues:{payload}".encode(), "sha256").hexdigest()[:32]


def _is_https(request: Request) -> bool:
    # Imported lazily: main imports the router that imports this module.
    from ..main import _is_https as is_https

    return is_https(request)
