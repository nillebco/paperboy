import asyncio
import io
import re
import zipfile
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from later_ink import main
from later_ink.matins import notify, webhooks
from later_ink.matins.db import MatinsStore
from later_ink.matins.extract import html_to_content, visible_text, word_count
from later_ink.matins.issue_epub import IssueItem, build_issue
from later_ink.matins.rss import clear_cache, parse_feed

SECRET_PAT = r"([a-z]+(?:-[a-z]+){3})"

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First story</title>
      <link>https://example.com/first</link>
      <guid>https://example.com/first</guid>
      <pubDate>Fri, 30 Oct 2026 06:00:00 GMT</pubDate>
      <description>Short summary of one.</description>
      <content:encoded><![CDATA[<p>%s</p>]]></content:encoded>
      <dc:creator>Ann Author</dc:creator>
    </item>
    <item>
      <title>Second story</title>
      <link>https://example.com/second</link>
      <pubDate>Fri, 30 Oct 2026 07:30:00 GMT</pubDate>
      <description>Another summary.</description>
    </item>
  </channel>
</rss>
""" % (b"Body text of the first story. " * 40)

ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <entry>
    <title>Atom story</title>
    <link rel="alternate" href="https://example.com/atom-story"/>
    <published>2026-10-30T05:00:00Z</published>
    <summary>Atom summary.</summary>
    <author><name>Bea Writer</name></author>
  </entry>
</feed>
"""

ARTICLE_HTML = """
<html><body>
<nav><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a></nav>
<article class="post">
  <h1>A real headline</h1>
  <p>{body}</p>
  <p>{more}</p>
</article>
<footer><a href="/x">X</a> Copyright nobody</footer>
</body></html>
""".format(body="Words in the body. " * 30, more="More words here. " * 30)


# ------------------------------------------------------------------ parsing


def test_parse_rss_items_are_stable_and_dated():
    first = parse_feed(RSS_XML, "https://example.com/feed", "abc123")
    second = parse_feed(RSS_XML, "https://example.com/feed", "abc123")

    assert first.title == "Example Feed"
    assert [i.title for i in first.items] == ["First story", "Second story"]
    # Re-parsing must produce the same ids: they are the dedupe key.
    assert [i.id for i in first.items] == [i.id for i in second.items]
    assert all(i.id.startswith("abc123-") for i in first.items)
    assert first.items[0].author == "Ann Author"
    assert first.items[0].published == datetime(2026, 10, 30, 6, 0)
    assert "Body text" in first.items[0].content_html


def test_parse_atom_entry():
    parsed = parse_feed(ATOM_XML, "https://example.com/atom", "def456")
    assert parsed.title == "Atom Feed"
    [item] = parsed.items
    assert item.title == "Atom story"
    assert item.link == "https://example.com/atom-story"
    assert item.author == "Bea Writer"
    assert item.published == datetime(2026, 10, 30, 5, 0)


def test_parse_rejects_non_feed():
    from later_ink.connectors.base import UpstreamError

    with pytest.raises(UpstreamError):
        parse_feed(b"<html><body>not a feed</body></html>", "https://example.com", "x")


# ---------------------------------------------------------------- extraction


def test_extract_prefers_article_over_navigation():
    content = html_to_content(ARTICLE_HTML, "https://example.com/post")
    assert content is not None
    text = visible_text(content)
    assert "A real headline" in text
    assert "Words in the body" in text
    assert "Copyright nobody" not in text


def test_extract_returns_none_on_thin_page():
    assert html_to_content("<html><body><p>Hi</p></body></html>", "https://e.com") is None


def test_word_count_ignores_markup():
    assert word_count("<p>one two <b>three</b></p>") == 3


# --------------------------------------------------------------------- store


def _store(tmp_path) -> MatinsStore:
    return MatinsStore(str(tmp_path / "app.db"), Fernet(Fernet.generate_key()))


def test_store_feeds_and_queue(tmp_path):
    store = _store(tmp_path)
    feed = store.add_feed("sec", "https://example.com/feed", "")
    assert store.list_feeds("sec") == [feed]
    # Same URL twice is one subscription.
    store.add_feed("sec", "https://example.com/feed", "Renamed")
    assert len(store.list_feeds("sec")) == 1
    assert store.list_feeds("sec")[0].title == "Renamed"

    assert store.save_url("sec", "https://example.com/a") is True
    assert store.save_url("sec", "https://example.com/a") is False
    assert [i.url for i in store.list_saved("sec")] == ["https://example.com/a"]

    saved = store.list_saved("sec")[0]
    store.consume_saved("sec", [saved.id], "issue-1")
    assert store.list_saved("sec") == []
    # Re-saving a consumed link puts it back in the queue.
    assert store.save_url("sec", "https://example.com/a") is True
    assert len(store.list_saved("sec")) == 1


def test_store_encrypts_webhook_secret(tmp_path):
    store = _store(tmp_path)
    store.set_delivery(
        "sec",
        webhook_url="https://example.com/hook",
        webhook_secret="super-secret-value",
        email="me@example.com",
        schedule="07:00",
        timezone="Europe/Paris",
        enabled=True,
    )
    delivery = store.get_delivery("sec")
    assert delivery is not None
    assert delivery.webhook_secret == "super-secret-value"
    raw = (tmp_path / "app.db").read_bytes()
    assert b"super-secret-value" not in raw


def test_store_tracks_issued_articles_and_days(tmp_path):
    store = _store(tmp_path)
    store.record_issue(
        issue_id="20261030-aaaaaa",
        secret="sec",
        title="Matins",
        local_day="2026-10-30",
        path=str(tmp_path / "x.epub"),
        article_count=1,
        word_count=10,
        items=[("feeds:abc", "Title", "https://example.com/a", "Feeds · Example")],
    )
    assert store.issued_articles("sec", ["feeds:abc", "other"]) == {"feeds:abc"}
    assert store.has_issue_for_day("sec", "2026-10-30")
    assert not store.has_issue_for_day("sec", "2026-10-31")


# --------------------------------------------------------------------- issue


def _items() -> list[IssueItem]:
    return [
        IssueItem(title="Saved one", html="<p>Saved body.</p>", section="Saved for later"),
        IssueItem(
            title="Feed one",
            html="<p>Feed body.</p>",
            section="Feeds · Example",
            source_url="https://example.com/one",
        ),
    ]


def test_build_issue_is_deterministic_and_has_both_sections():
    date = datetime(2026, 10, 30, 7, 0)
    first = asyncio.run(build_issue(title="Matins", issue_date=date, items=_items()))
    second = asyncio.run(build_issue(title="Matins", issue_date=date, items=_items()))

    with zipfile.ZipFile(io.BytesIO(first.result.data)) as zf:
        chapters = sorted(n for n in zf.namelist() if n.startswith("EPUB/item_"))
        assert len(chapters) == 2
        body = "".join(zf.read(name).decode() for name in chapters)
        nav = zf.read("EPUB/nav.xhtml").decode()
    assert "Saved body." in body and "Feed body." in body
    assert "Saved for later" in nav and "Feeds · Example" in nav
    # Same issue built twice must be byte-identical: KOReader matches documents
    # by content hash for reading-progress sync.
    assert first.result.data == second.result.data


def test_two_catalogues_can_share_a_feed_url():
    """The parsed-feed cache must not leak one catalogue's item ids into
    another's: every download would 404 with "feed no longer subscribed"."""
    import tempfile

    from cryptography.fernet import Fernet

    from later_ink.matins.rss import RssConnector

    with tempfile.TemporaryDirectory() as tmp:
        store = MatinsStore(f"{tmp}/app.db", Fernet(Fernet.generate_key()))
        secret_a, secret_b = "cat-a", "cat-b"
        feed_a = store.add_feed(secret_a, "https://example.com/feed")
        feed_b = store.add_feed(secret_b, "https://example.com/feed")
        assert feed_a.id != feed_b.id

        async def fake_fetch(url, *, timeout, max_bytes):
            return RSS_XML

        import later_ink.matins.rss as rss_module

        original = rss_module.fetch_feed
        rss_module.fetch_feed = fake_fetch
        clear_cache()
        try:
            a = RssConnector(store, secret_a)
            b = RssConnector(store, secret_b)
            articles_a, _ = asyncio.run(a.list_articles(feed_a.id))
            articles_b, _ = asyncio.run(b.list_articles(feed_b.id))
            assert articles_a[0].id != articles_b[0].id
            # Each connector must resolve its own article id.
            _, html_a = asyncio.run(a.get_article_html(articles_a[0].id))
            _, html_b = asyncio.run(b.get_article_html(articles_b[0].id))
            assert "Body text" in html_a and "Body text" in html_b
        finally:
            rss_module.fetch_feed = original
            clear_cache()


def test_issue_images_do_not_collide_between_articles():
    """Every document numbers its images from zero; in one book that means
    chapter two silently overwrites chapter one's first image."""
    import httpx

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    items = [
        IssueItem(
            title=f"Article {n}",
            html='<p>Body.</p><img src="https://example.com/pic.png">',
            section="Feeds",
        )
        for n in (1, 2)
    ]
    built = asyncio.run(
        build_issue(
            title="Matins", issue_date=datetime(2026, 10, 30, 7, 0), items=items, image_client=client
        )
    )
    with zipfile.ZipFile(io.BytesIO(built.result.data)) as zf:
        images = sorted(n for n in zf.namelist() if n.startswith("EPUB/images/"))
        chapters = [zf.read(n).decode() for n in zf.namelist() if n.startswith("EPUB/item_")]
    assert len(images) == 2, images
    assert len(set(images)) == 2
    # Each chapter points at its own file, not at a shared one.
    assert "c000-img0.png" in chapters[0]
    assert "c001-img0.png" in chapters[1]


# ------------------------------------------------------------------ webhooks


def _body() -> bytes:
    return b'{"event":"issue.ready"}'


def test_signature_round_trip_and_rejections():
    import time

    now = int(time.time())
    header = webhooks.signature_header("s3cret", now, _body())
    assert webhooks.verify(header, "s3cret", _body())
    assert not webhooks.verify(header, "wrong", _body())
    assert not webhooks.verify(header, "s3cret", _body() + b"x")
    stale = webhooks.signature_header("s3cret", now - 3600, _body())
    assert not webhooks.verify(stale, "s3cret", _body())


def test_deliver_signs_and_posts(monkeypatch):
    calls: list[dict] = []

    class FakeResponse:
        status_code = 204

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, content=None, headers=None, timeout=None):
            calls.append({"url": url, "content": content, "headers": headers})
            return FakeResponse()

    async def fake_validate(_url):
        return None

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(webhooks, "_validate", fake_validate)

    ok = asyncio.run(
        webhooks.deliver(
            "https://example.com/hook", "s3cret", {"event": "issue.ready"}, allow_private=False
        )
    )
    assert ok
    [call] = calls
    assert call["url"] == "https://example.com/hook"
    header = call["headers"][webhooks.SIGNATURE_HEADER]
    assert webhooks.verify(header, "s3cret", call["content"])


def test_deliver_refuses_private_targets_by_default():
    ok = asyncio.run(
        webhooks.deliver("http://127.0.0.1:9000/hook", None, {"event": "x"}, allow_private=False)
    )
    assert ok is False


def test_notification_is_a_link_not_an_attachment():
    class Issue:
        title = "Matins — Friday 30 October 2026"
        article_count = 12
        word_count = 6000

    subject = notify.issue_subject(Issue())
    body = notify.issue_body(Issue(), "https://matins.example/abcd/")
    assert "12" in subject
    assert "https://matins.example/abcd/" in body
    assert "attached" in body


# ------------------------------------------------------------------ the app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from later_ink.connectors import readwise

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MATINS_SCHEDULER", "0")
    monkeypatch.setenv("BASE_URL", "https://matins.example")
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    clear_cache()

    async def fake_fetch_feed(url, *, timeout, max_bytes):
        return RSS_XML

    async def fake_page(url):
        return ARTICLE_HTML

    sent_codes: dict[str, str] = {}

    async def fake_send_code(recipient, code, **_kwargs):
        sent_codes[recipient] = code
        return True

    monkeypatch.setattr("later_ink.matins.notify.send_code", fake_send_code)

    monkeypatch.setattr("later_ink.matins.rss.fetch_feed", fake_fetch_feed)
    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake_page)

    async def fake_validate(_token):
        return True

    monkeypatch.setattr(readwise, "validate_token", fake_validate)

    with TestClient(app=main.app) as c:
        c.sent_codes = sent_codes
        yield c


def _signup_without_readwise(client) -> str:
    resp = client.post("/start", data={"readwise_token": ""})
    assert resp.status_code == 200
    match = re.search(rf"/{SECRET_PAT}/regenerate", resp.text)
    assert match
    return match.group(1)


def _add_feed(client, secret: str, url: str = "https://example.com/feed"):
    csrf = _csrf(client, secret)
    resp = client.post(
        f"/{secret}/feeds", data={"csrf": csrf, "url": url}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    return resp.headers["location"]


def _csrf(client, secret: str) -> str:
    page = client.get(f"/{secret}/settings")
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text)
    assert match
    return match.group(1)


def test_catalogue_can_be_created_without_readwise(client):
    secret = _signup_without_readwise(client)
    root = client.get(f"/{secret}/")
    assert root.status_code == 200
    assert "Issues" in root.text
    assert "Saved" in root.text


def test_settings_saves_delivery(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)

    resp = client.post(
        f"/{secret}/settings",
        data={
            "csrf": csrf,
            "email": "me@example.com",
            "webhook_url": "https://example.com/hook",
            "webhook_secret": "whsec",
            "schedule": "07:15",
            "timezone": "Europe/Paris",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get(f"/{secret}/settings").text
    assert "07:15" in page
    assert "me@example.com" in page
    delivery = client.app.state.matins.get_delivery(secret)
    assert delivery is not None
    assert delivery.email == "me@example.com"
    assert delivery.webhook_secret == "whsec"


def test_settings_rejects_a_bad_timezone_and_keeps_the_rest(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    resp = client.post(
        f"/{secret}/settings",
        data={
            "csrf": csrf,
            "email": "me@example.com",
            "schedule": "07:15",
            "timezone": "Nowhere/Nope",
        },
    )
    assert resp.status_code == 400
    assert "Nowhere/Nope" in resp.text
    # The other field the user typed survives the re-render.
    assert "me@example.com" in resp.text


def test_settings_render_after_saving_blank_optional_fields(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    resp = client.post(
        f"/{secret}/settings",
        data={
            "csrf": csrf,
            "email": "",
            "webhook_url": "",
            "webhook_secret": "",
            "schedule": "07:00",
            "timezone": "Europe/Paris",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Blank optional fields are stored as NULL; rendering the page that saved
    # them must not blow up on escape(None).
    page = client.get(f"/{secret}/settings?saved=1")
    assert page.status_code == 200
    assert "Webhook" in page.text


def test_save_endpoint_queues_a_link(client):
    secret = _signup_without_readwise(client)
    resp = client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    assert resp.status_code == 201
    assert resp.json()["queued"] is True

    again = client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    assert again.status_code == 200
    assert again.json()["queued"] is False

    bad = client.post(f"/{secret}/save", json={"url": "javascript:alert(1)"})
    assert bad.status_code == 400

    feed = client.get(f"/{secret}/saved:inbox/")
    assert feed.status_code == 200
    assert "https://example.com/later" in feed.text


def test_compose_now_builds_a_downloadable_issue(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/settings",
        data={
            "csrf": csrf,
            "feeds": "https://example.com/feed",
            "schedule": "07:00",
            "timezone": "Europe/Paris",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})

    built = client.post(
        f"/{secret}/issues/build", data={"csrf": csrf}, follow_redirects=False
    )
    assert built.status_code == 200
    assert "Composed" in built.text

    shelf = client.get(f"/{secret}/issues/")
    assert shelf.status_code == 200
    assert "Matins —" in shelf.text

    issue = client.app.state.matins.list_issues(secret)[0]
    download = client.get(f"/{secret}/issues/{issue.id}.epub")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/epub+zip"
    assert download.content[:2] == b"PK"

    # A second compose finds nothing new: the feed item is not reprinted and the
    # saved link is consumed.
    again = client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    assert "Nothing new" in again.text


def test_issue_download_is_scoped_to_its_catalogue(client):
    first = _signup_without_readwise(client)
    second = _signup_without_readwise(client)
    csrf = _csrf(client, first)
    _add_feed(client, first)
    client.post(f"/{first}/issues/build", data={"csrf": csrf})
    issue = client.app.state.matins.list_issues(first)[0]

    assert client.get(f"/{second}/issues/{issue.id}.epub").status_code == 404


def test_deleting_a_catalogue_removes_its_feeds_queue_and_issues(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    issue = client.app.state.matins.list_issues(secret)[0]
    from pathlib import Path

    path = Path(issue.path)
    assert path.exists()

    resp = client.post(f"/{secret}/delete", data={"csrf": csrf})
    assert resp.status_code == 200
    store = client.app.state.matins
    assert store.list_feeds(secret) == []
    assert store.list_saved(secret, include_consumed=True) == []
    assert store.list_issues(secret) == []
    assert store.get_delivery(secret) is None
    assert not path.exists()
    assert not path.parent.exists()


def test_browser_gets_a_dashboard_and_readers_get_opds(client):
    secret = _signup_without_readwise(client)

    html = client.get(f"/{secret}/", headers={"accept": "text/html"})
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "Compose now" in html.text
    assert f"/{secret}/settings" in html.text
    assert f"/{secret}/issues/preview" in html.text
    assert f"/{secret}/read" in html.text

    # A reader asking for nothing (or for Atom) still gets the catalogue.
    feed = client.get(f"/{secret}/", headers={"accept": "*/*"})
    assert feed.headers["content-type"].startswith("application/atom+xml")
    assert "<feed" in feed.text
    atom = client.get(f"/{secret}/", headers={"accept": "application/atom+xml"})
    assert "<feed" in atom.text


def test_landing_lists_the_catalogues_this_browser_has(client):
    secret = _signup_without_readwise(client)
    landing = client.get("/")
    assert "Your catalogues on this device" in landing.text
    assert f'href="/{secret}/"' in landing.text

    client.get(f"/{secret}/", headers={"accept": "text/html"})
    csrf = _csrf(client, secret)
    forgotten = client.post(
        f"/{secret}/forget", data={"csrf": csrf}, follow_redirects=False
    )
    assert forgotten.status_code == 303
    assert secret not in client.get("/").text
    # Forgetting the browser entry does not touch the catalogue itself.
    assert client.get(f"/{secret}/").status_code == 200


def test_preview_and_issue_use_the_feed_title_not_its_url(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    _add_feed(client, secret)
    preview = client.get(f"/{secret}/issues/preview")
    assert "Example Feed" in preview.text
    assert "https://example.com/feed" not in preview.text

    client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    with client.app.state.matins._conn() as conn:
        sections = {r["section"] for r in conn.execute("select section from issue_items")}
    assert sections == {"Example Feed"}


def test_preview_shows_the_selection_without_composing(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)

    empty = client.get(f"/{secret}/issues/preview")
    assert empty.status_code == 200
    assert "Nothing is queued" in empty.text

    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})

    preview = client.get(f"/{secret}/issues/preview")
    assert preview.status_code == 200
    assert "First story" in preview.text
    assert "Saved for later" in preview.text
    assert "Read later" in preview.text or "later" in preview.text.lower()
    # Previewing must not create an issue or consume the queue.
    assert client.app.state.matins.list_issues(secret) == []
    assert len(client.app.state.matins.list_saved(secret)) == 1

    composed = client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    assert "Composed" in composed.text


def test_admin_run_requires_token(client, monkeypatch):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    _add_feed(client, secret)
    client.post(
        f"/{secret}/settings",
        data={"csrf": csrf, "schedule": "07:00", "enabled": "1"},
        follow_redirects=False,
    )

    assert client.post("/admin/issues/run").status_code == 404

    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    assert client.post("/admin/issues/run", headers={"x-admin-token": "nope"}).status_code == 403
    resp = client.post(
        "/admin/issues/run", headers={"x-admin-token": "admin-secret"}, params={"force": 1}
    )
    assert resp.status_code == 200
    assert len(resp.json()["built"]) == 1


def test_scheduler_is_due_after_scheduled_time():
    from later_ink.matins import scheduler

    assert scheduler._due("07:00", "Europe/Paris", datetime(2026, 10, 30, 6, 30, tzinfo=UTC))
    assert not scheduler._due("07:00", "Europe/Paris", datetime(2026, 10, 30, 4, 0, tzinfo=UTC))
    assert not scheduler._due("garbage", "Europe/Paris", datetime(2026, 10, 30, 9, 0, tzinfo=UTC))
    assert not scheduler._due("07:00", "Nowhere/Nope", datetime(2026, 10, 30, 9, 0, tzinfo=UTC))


# ------------------------------------------------------------------ sign-in


def test_signin_creates_and_recovers_the_same_catalogue(client):
    resp = client.post("/signin", data={"email": "Ivo@Example.com"})
    assert resp.status_code == 200
    assert "Check your email" in resp.text
    code = client.sent_codes["ivo@example.com"]
    assert len(code) == 6 and code.isdigit()

    wrong = client.post(
        "/signin/code", data={"email": "ivo@example.com", "code": "000000"}
    )
    assert wrong.status_code == 400

    ok = client.post(
        "/signin/code", data={"email": "ivo@example.com", "code": code},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    first = ok.headers["location"]

    # A different browser, same address: same catalogue, no link to keep.
    other = TestClient(app=main.app)
    client.sent_codes.clear()
    other.post("/signin", data={"email": "ivo@example.com"})
    again = other.post(
        "/signin/code",
        data={"email": "ivo@example.com", "code": client.sent_codes["ivo@example.com"]},
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert again.headers["location"] == first


def test_signin_codes_expire_and_are_rate_limited(client):
    client.post("/signin", data={"email": "ivo@example.com"})
    code = client.sent_codes["ivo@example.com"]

    with client.app.state.matins._conn() as conn:
        conn.execute("UPDATE otp SET expires_at = 0 WHERE email = ?", ("ivo@example.com",))
    expired = client.post("/signin/code", data={"email": "ivo@example.com", "code": code})
    assert "expired" in expired.text

    # A live challenge that has already spent its attempts is refused outright.
    import time as _time

    with client.app.state.matins._conn() as conn:
        conn.execute(
            "UPDATE otp SET expires_at = ?, attempts = 5 WHERE email = ?",
            (_time.time() + 600, "ivo@example.com"),
        )
    blocked = client.post(
        "/signin/code", data={"email": "ivo@example.com", "code": "111111"}
    )
    assert blocked.status_code == 429


def test_signin_does_not_say_whether_an_address_exists(client):
    # Same shape for an unknown address and one with a catalogue.
    client.post("/signin", data={"email": "nobody@example.com"})
    unknown = client.post("/signin", data={"email": "nobody@example.com"})
    _secret = _signup_without_readwise(client)
    known = client.post("/signin", data={"email": "nobody@example.com"})
    assert unknown.status_code == known.status_code == 200
    assert "Check your email" in unknown.text and "Check your email" in known.text


def test_a_catalogue_saved_with_an_email_can_be_found_by_code(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/settings",
        data={
            "csrf": csrf,
            "email": "ivo@example.com",
            "schedule": "07:00",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    other = TestClient(app=main.app)
    client.sent_codes.clear()
    other.post("/signin", data={"email": "ivo@example.com"})
    landed = other.post(
        "/signin/code",
        data={"email": "ivo@example.com", "code": client.sent_codes["ivo@example.com"]},
        follow_redirects=False,
    )
    assert landed.headers["location"] == f"/{secret}/"


# ------------------------------------------------------------------- reader


def test_reader_lists_items_and_opening_one_marks_it_read(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})

    page = client.get(f"/{secret}/read")
    assert page.status_code == 200
    assert "First story" in page.text
    assert "Saved for later" in page.text
    # The list is newest-first, so pick the item by title rather than position.
    match = re.search(
        rf'href="/{re.escape(secret)}/read/([^"]+)"[^>]*>First story', page.text
    )
    assert match, page.text
    article_id = match.group(1)
    assert article_id.startswith("feeds:")
    opened = client.get(f"/{secret}/read/{article_id}")
    assert opened.status_code == 200
    assert "Body text of the first story" in opened.text
    assert "script" not in opened.text.lower()

    after = client.get(f"/{secret}/read")
    assert f"/read/{article_id}" not in after.text
    everything = client.get(f"/{secret}/read?all=1")
    assert f"/read/{article_id}" in everything.text

    client.post(f"/{secret}/read/{article_id}/unread", data={"csrf": _csrf(client, secret)})
    assert f"/read/{article_id}" in client.get(f"/{secret}/read").text


def test_reader_article_html_is_sanitised():
    from later_ink.matins.reader import to_page_html

    out = to_page_html(
        '<p>ok</p><script>alert(1)</script>'
        '<a href="javascript:alert(1)">bad</a>'
        '<a href="https://example.com/x">good</a>'
        '<img src="https://example.com/i.png" onerror="alert(1)">'
    )
    assert "script" not in out.lower()
    assert "javascript:" not in out
    assert "onerror" not in out
    assert 'target="_blank"' in out


# ------------------------------------------------------------------- sources


def test_adding_a_feed_validates_it_immediately(client, monkeypatch):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)

    bad_url = client.post(
        f"/{secret}/feeds",
        data={"csrf": csrf, "url": "not a url"},
        follow_redirects=False,
    )
    assert bad_url.status_code == 303
    assert "error=" in bad_url.headers["location"]

    async def html_body(url, *, timeout, max_bytes):
        return b"<html><body>not a feed</body></html>"

    monkeypatch.setattr("later_ink.matins.rss.fetch_feed", html_body)
    not_feed = client.post(
        f"/{secret}/feeds",
        data={"csrf": csrf, "url": "https://example.com/page"},
        follow_redirects=False,
    )
    assert "error=" in not_feed.headers["location"]
    assert client.app.state.matins.list_feeds(secret) == []


def test_removing_a_feed_and_a_saved_link(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    csrf = _csrf(client, secret)

    feed = client.app.state.matins.list_feeds(secret)[0]
    client.post(f"/{secret}/feeds/{feed.id}/delete", data={"csrf": csrf})
    assert client.app.state.matins.list_feeds(secret) == []

    saved = client.app.state.matins.list_saved(secret)[0]
    client.post(f"/{secret}/saved/{saved.id}/delete", data={"csrf": csrf})
    assert client.app.state.matins.list_saved(secret) == []
