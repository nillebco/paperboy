"""Server-side page counting: what is sent, and above all what never is.

The catalogue secret is the credential and sits in the URL path, so the
collector must only ever see the route template.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from later_ink import analytics, main

COLLECT = "https://analytics.example/api/v1/analytics/collect?property_id=p1"


@pytest.fixture()
def sent(monkeypatch):
    calls: list[tuple[str, dict, dict]] = []

    async def fake_post(url, payload, headers):
        calls.append((url, payload, headers))

    monkeypatch.setattr(analytics, "_post", fake_post)
    return calls


@pytest.fixture()
def client(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MATINS_SCHEDULER", "0")
    monkeypatch.setenv("BASE_URL", "https://mynews.example")
    monkeypatch.setenv("ANALYTICS_COLLECT_URL", COLLECT)
    with TestClient(app=main.app) as c:
        yield c


def _drain(client):
    # The tasks run on the TestClient's portal loop; one round trip lets them finish.
    client.portal.call(asyncio.sleep, 0)


def test_landing_page_is_a_page_view(client, sent):
    client.get(
        "/",
        headers={
            "user-agent": "Mozilla/5.0 test",
            "accept-language": "fr-FR",
            "referer": "https://news.ycombinator.com/item?id=1",
        },
    )
    _drain(client)
    assert len(sent) == 1
    url, payload, headers = sent[0]
    assert url == COLLECT
    assert payload == {
        "page_url": "https://mynews.example/",
        "referrer": "https://news.ycombinator.com/item",
        "event_type": "page_view",
    }
    assert headers["User-Agent"] == "Mozilla/5.0 test"
    assert headers["Accept-Language"] == "fr-FR"
    assert headers["X-Forwarded-For"]


def test_catalogue_secret_never_leaves(client, sent):
    resp = client.post("/start", data={"readwise_token": ""})
    assert resp.status_code == 200
    secret = resp.text.split("/regenerate")[0].rsplit("/", 1)[-1]
    sent.clear()

    client.get(f"/{secret}/", headers={"referer": f"https://mynews.example/{secret}/settings"})
    client.get(f"/{secret}/settings")
    _drain(client)

    assert sent, "catalogue pages are counted"
    for _url, payload, _headers in sent:
        assert secret not in repr(payload)
        assert "{secret}" in payload["page_url"]
        assert payload["referrer"] is None  # own site is not a referrer


def test_machinery_and_failures_are_not_counted(client, sent):
    client.get("/healthz")
    client.get("/assets/og.png")
    client.get("/no-such-page/at/all")
    client.get("/", headers={"host": "www.mynews.example"}, follow_redirects=False)
    _drain(client)
    assert sent == []


def test_off_without_collect_url(client, sent, monkeypatch):
    monkeypatch.delenv("ANALYTICS_COLLECT_URL")
    client.get("/")
    _drain(client)
    assert sent == []


@pytest.mark.parametrize(
    ("method", "path", "status", "ctype", "expected"),
    [
        ("GET", "/{secret}/", 200, "application/atom+xml;profile=opds-catalog", "opds"),
        ("GET", "/{secret}/issues/{issue_id}.epub", 200, "application/epub+zip", "download"),
        ("GET", "/{secret}/read", 200, "text/html; charset=utf-8", "page_view"),
        ("HEAD", "/{secret}/", 200, "application/atom+xml", None),
        ("GET", "/{secret}/", 404, "text/html", None),
        ("GET", "/api/v1/items", 200, "application/json", None),
        ("GET", "/{secret}/read/speech/{key}.mp3", 200, "audio/mpeg", None),
    ],
)
def test_event_type(method, path, status, ctype, expected):
    assert analytics.event_type(method, path, status, ctype) == expected
