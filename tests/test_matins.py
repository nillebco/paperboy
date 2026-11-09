import asyncio
import io
import re
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote

import httpx
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


def test_extract_flags_a_blocked_page():
    from later_ink.matins.extract import looks_blocked

    wall = (
        "<html><body><p>A required part of this site couldn't load. This may be "
        "due to a browser extension, network issues, or browser settings.</p>"
        "</body></html>"
    )
    assert looks_blocked(wall)
    assert not looks_blocked(ARTICLE_HTML)


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

    issue_url = "https://matins.example/abcd/issues/20261030-ab"
    subject = notify.issue_subject(Issue())
    body = notify.issue_body(Issue(), "https://matins.example/abcd/", issue_url)
    assert "12" in subject
    assert issue_url in body
    assert "https://matins.example/abcd/" in body
    assert "attached" in body


def test_issue_payload_links_to_the_issue_and_its_epub():
    class Issue:
        id = "20261030-ab"
        title = "Matins — Friday 30 October 2026"
        article_count = 3
        word_count = 600
        created_at = 1793000000

    payload = webhooks.issue_payload(Issue(), "https://matins.example/abcd/issues/")
    assert payload["issue_url"] == "https://matins.example/abcd/issues/20261030-ab"
    assert payload["issue"]["web_url"] == payload["issue_url"]
    assert (
        payload["issue"]["download_url"]
        == "https://matins.example/abcd/issues/20261030-ab.epub"
    )


# ------------------------------------------------------------------ the app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from later_ink.connectors import readwise

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("ALLOW_FREE_SIGNUP", "1")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("MATINS_SCHEDULER", "0")
    # The fixture's feed has fixed dates; a wide window keeps freshness checks
    # from turning the suite red as the wall clock moves past them.
    monkeypatch.setenv("ISSUE_WINDOW_HOURS", "720")
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
    assert built.status_code == 303
    landed = client.get(built.headers["location"])
    assert "Composed" in landed.text
    assert ".epub" in landed.text

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


def test_dashboard_offers_the_catalogue_address_to_copy(client):
    secret = _signup_without_readwise(client)
    page = client.get(f"/{secret}/", headers={"accept": "text/html"})

    base = "https://matins.example"
    # Both addresses are plain text (selectable without script) and each has a
    # copy button aimed at it.
    assert f'id="catalog-url">{base}/{secret}/</span>' in page.text
    assert f'id="issues-url">{base}/{secret}/issues/</span>' in page.text
    assert 'data-copy="#catalog-url">Copy address</button>' in page.text
    assert 'data-copy="#issues-url">Copy</button>' in page.text

    # The button is inert without its script, and the script is allowed by hash.
    from later_ink import pages as shared_pages
    from later_ink.matins import pages as matins_pages

    assert f"<script>{matins_pages.COPY_JS}</script>" in page.text
    csp = page.headers["content-security-policy"]
    assert shared_pages._script_hash(matins_pages.COPY_JS) in csp

    settings = client.get(f"/{secret}/settings").text
    assert 'data-copy="#catalog-url">Copy address</button>' in settings
    assert f"<script>{matins_pages.COPY_JS}</script>" in settings


def test_dashboard_prompts_for_an_email_until_one_is_linked(client):
    secret = _signup_without_readwise(client)
    before = client.get(f"/{secret}/", headers={"accept": "text/html"})
    assert "Keep this catalogue recoverable" in before.text
    assert f"/{secret}/settings" in before.text

    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/settings",
        data={"csrf": csrf, "email": "ivo@example.com", "schedule": "07:00", "enabled": "1"},
        follow_redirects=False,
    )
    after = client.get(f"/{secret}/", headers={"accept": "text/html"})
    assert "Keep this catalogue recoverable" not in after.text
    assert "ivo@example.com" in after.text


def test_an_email_cannot_open_two_catalogues(client):
    first = _signup_without_readwise(client)
    second = _signup_without_readwise(client)
    for secret in (first, second):
        csrf = _csrf(client, secret)
        client.post(
            f"/{secret}/settings",
            data={"csrf": csrf, "email": "ivo@example.com", "schedule": "07:00"},
            follow_redirects=False,
        )
    assert client.app.state.matins.secret_for_email("ivo@example.com") == first
    # The second attempt is refused and says why, rather than silently stealing
    # an address that already opens another catalogue.
    csrf = _csrf(client, second)
    resp = client.post(
        f"/{second}/settings",
        data={"csrf": csrf, "email": "ivo@example.com", "schedule": "07:00"},
    )
    assert resp.status_code == 400
    assert "already opens another catalogue" in resp.text


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
        conn.execute(
            "UPDATE otp SET expires_at = 0 WHERE identity = ?", ("ivo@example.com",)
        )
    expired = client.post("/signin/code", data={"email": "ivo@example.com", "code": code})
    assert "expired" in expired.text

    # A live challenge that has already spent its attempts is refused outright.
    import time as _time

    with client.app.state.matins._conn() as conn:
        conn.execute(
            "UPDATE otp SET expires_at = ?, attempts = 5 WHERE identity = ?",
            (_time.time() + 600, "ivo@example.com"),
        )
    blocked = client.post(
        "/signin/code", data={"email": "ivo@example.com", "code": "111111"}
    )
    assert blocked.status_code == 429


def test_one_mailbox_is_one_identity_however_it_is_spelled():
    from later_ink.matins.identity import identity_key

    # Gmail ignores dots; every provider that supports subaddressing ignores +tag.
    assert identity_key("A.B+matins@Gmail.com") == identity_key("ab@gmail.com")
    assert identity_key("ivo+matins@example.com") == identity_key("ivo@example.com")
    # A provider whose dots mean something keeps them.
    assert identity_key("i.vo@example.com") != identity_key("ivo@example.com")
    # A local part that is only "+something" is not subaddressing, and an address
    # that cannot be split still gets a key of its own.
    assert identity_key("+tag@example.com") == "+tag@example.com"
    assert identity_key("not-an-address") == "not-an-address"
    assert identity_key("") == ""


def test_a_respelled_gmail_address_cannot_get_a_second_code(client):
    """The probe of 6-7 November: dots were a free extra budget per mailbox."""
    first = client.post("/signin", data={"email": "af.u.c.aj.a.xor.5.84@gmail.com"})
    assert "Check your email" in first.text
    second = client.post("/signin", data={"email": "afucajaxor584@gmail.com"})
    assert "A code was just sent" in second.text


def test_a_code_works_whichever_spelling_the_reader_retypes(client):
    client.post("/signin", data={"email": "a.b@gmail.com"})
    code = client.sent_codes["a.b@gmail.com"]

    verified = client.post(
        "/signin/code",
        data={"email": "ab@gmail.com", "code": code},
        follow_redirects=False,
    )

    assert verified.status_code == 303


def test_signing_in_again_keeps_the_address_already_on_file(client):
    store = client.app.state.matins
    secret = _signup_without_readwise(client)
    store.link_email(secret, "me+matins@gmail.com")

    store.link_email(secret, "me@gmail.com")

    # The +tag is a filing rule, not a typo: the mailbox is the same one.
    assert store.email_for_secret(secret) == "me+matins@gmail.com"


def test_sign_in_is_also_limited_per_connection(client):
    from later_ink.ratelimit import DurableLimiter

    client.app.state.signin_limiter = DurableLimiter(
        client.app.state.store, bucket="signin-test", limit=1, window=3600.0
    )

    first = client.post("/signin", data={"email": "one@example.com"})
    second = client.post("/signin", data={"email": "two@example.com"})

    assert "Check your email" in first.text
    assert second.status_code == 429
    assert "Too many codes requested from this connection" in second.text


def test_an_abandoned_challenge_is_swept_a_day_after_it_expires(client):
    import time as _time

    store = client.app.state.matins
    store.set_otp("gone@example.com", "gone@example.com", "x", ttl=1)
    store.set_otp("live@example.com", "live@example.com", "y", ttl=3600)
    with store._conn() as conn:
        conn.execute(
            "UPDATE otp SET expires_at = ? WHERE identity = ?",
            (_time.time() - 2 * 86400, "gone@example.com"),
        )

    removed = store.purge_expired_otp()

    assert removed == 1
    assert store.otp_state("gone@example.com") is None
    assert store.otp_state("live@example.com") is not None


def test_a_challenge_keeps_the_address_it_was_sent_to(client):
    client.post("/signin", data={"email": "Ivo.B@Example.com"})

    state = client.app.state.matins.otp_state("ivo.b@example.com")

    assert state is not None
    assert state["identity"] == "ivo.b@example.com"
    # What the reader typed, so the code goes to the address they own.
    assert state["address"] == "ivo.b@example.com"


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


def _reader_article_id(client, secret: str, title: str) -> str:
    page = client.get(f"/{secret}/read")
    match = re.search(
        rf'href="/{re.escape(secret)}/read\?view=all&article=([^"]+)"[^>]*>{re.escape(title)}',
        page.text,
    )
    assert match, page.text
    return unquote(match.group(1))


def test_queueing_does_not_move_a_row_in_the_list(client):
    """The next issue is a mark, not a sort order: the queue copy is dated
    "now", so sorting before collapsing duplicates lifted a row to the top."""
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)

    def titles() -> list[str]:
        page = client.get(f"/{secret}/read?view=all").text
        return re.findall(r'class="row-title"[^>]*>([^<]+)</a>', page)

    before = titles()
    assert before == ["Second story", "First story"]

    article_id = _reader_article_id(client, secret, "First story")
    client.post(
        f"/{secret}/read/queue",
        data={
            "csrf": _csrf(client, secret),
            "article_id": article_id,
            "view": "all",
        },
        follow_redirects=False,
    )

    assert titles() == before
    queued = client.get(f"/{secret}/read?view=all").text
    # The queued row says so in place: tinted class plus a badge.
    assert re.search(
        r'class="row queued" data-id="[^"]+">\s*<div class="row-main">\s*'
        r'<a class="row-title"[^>]*>First story</a>\s*'
        r'<span class="row-sub">Example Feed(?: · \d+ \w+ \d{4})? · \d+ minute'
        r'<span class="row-badge">Next issue</span></span>',
        queued,
    )


def test_reader_lists_items_and_opening_one_marks_it_read(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})

    page = client.get(f"/{secret}/read")
    assert page.status_code == 200
    assert "no-store" in page.headers["cache-control"]
    assert "First story" in page.text
    assert "Saved for later" in page.text
    article_id = _reader_article_id(client, secret, "First story")
    assert article_id.startswith("feeds:")

    # The article opens beside the list, not on its own page.
    opened = client.get(
        f"/{secret}/read?view=all&article={quote(article_id, safe='')}"
    )
    assert opened.status_code == 200
    assert "Body text of the first story" in opened.text
    assert "Saved for later" in opened.text
    # The only scripts are our own three (scroll, row actions, listen); the
    # article adds none.
    assert opened.text.count("<script>") == 3
    assert "First story" not in client.get(f"/{secret}/read?view=unread").text
    # A read row is visually distinct: the CSS hangs off this class.
    assert 'class="row read" data-id=' in client.get(f"/{secret}/read?view=all").text
    assert 'class="row read" data-id=' not in page.text

    client.post(
        f"/{secret}/read/{quote(article_id, safe=':-_')}/unread",
        data={"csrf": _csrf(client, secret)},
    )
    assert "First story" in client.get(f"/{secret}/read?view=unread").text


def test_reader_row_actions_do_not_open_the_article(client):
    """A like or + Next from the list keeps whatever is open, so a phone does
    not pop the article overlay; the pane's own buttons stay on the article."""
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article_id = _reader_article_id(client, secret, "First story")
    csrf = _csrf(client, secret)

    listing = client.get(f"/{secret}/read?view=all").text
    assert 'name="from_list" value="1"' in listing
    for path, extra in (("feedback", {"value": "like"}), ("queue", {}), ("unqueue", {})):
        response = client.post(
            f"/{secret}/read/{path}",
            data={"csrf": csrf, "article_id": article_id, "view": "all", "from_list": "1", **extra},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "article=" not in response.headers["location"]

    from_pane = client.post(
        f"/{secret}/read/feedback",
        data={"csrf": csrf, "article_id": article_id, "value": "clear", "view": "all"},
        follow_redirects=False,
    )
    assert "article=" in from_pane.headers["location"]


def test_reader_like_and_next_issue_are_stateful(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article_id = _reader_article_id(client, secret, "First story")
    csrf = _csrf(client, secret)

    # Unliked: the pane shows the same empty heart as the list row.
    pane = client.get(
        f"/{secret}/read?view=all&article={quote(article_id, safe='')}"
    )
    assert "♡ Like" in pane.text

    liked = client.post(
        f"/{secret}/read/feedback",
        data={"csrf": csrf, "article_id": article_id, "value": "like", "view": "liked"},
        follow_redirects=False,
    )
    assert liked.status_code == 303
    assert "First story" in client.get(f"/{secret}/read?view=liked").text
    pane = client.get(
        f"/{secret}/read?view=all&article={quote(article_id, safe='')}"
    )
    assert "♥ Liked" in pane.text
    signals = client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    assert [(s["url"], s["rating"]) for s in signals] == [
        ("https://example.com/first", "interesting")
    ]

    queued = client.post(
        f"/{secret}/read/queue",
        data={"csrf": csrf, "article_id": article_id, "view": "queued"},
        follow_redirects=False,
    )
    assert queued.status_code == 303
    assert "First story" in client.get(f"/{secret}/read?view=queued").text
    assert "https://example.com/first" in [
        saved.url for saved in client.app.state.matins.list_saved(secret)
    ]

    client.post(
        f"/{secret}/read/unqueue",
        data={"csrf": csrf, "article_id": article_id, "view": "queued"},
        follow_redirects=False,
    )
    assert "First story" not in client.get(f"/{secret}/read?view=queued").text


def test_a_blocked_article_falls_back_to_the_feed_summary(client, monkeypatch):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    # The second item has no content:encoded, so the connector must try the page.
    article_id = _reader_article_id(client, secret, "Second story")

    async def blocked(_self, _url, *, cookie=None):
        return (
            "<html><body><p>A required part of this site couldn't load. "
            "Please check your connection, disable any ad blockers, or try "
            "using a different browser.</p></body></html>"
        )

    monkeypatch.setattr("later_ink.matins.rss.RssConnector._fetch_page", blocked)
    page = client.get(
        f"/{secret}/read", params={"view": "all", "article": article_id}
    )
    assert "Another summary." in page.text
    assert "required part of this site" not in page.text


def test_reading_time_is_hidden_when_only_a_summary_is_known(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)

    page = client.get(f"/{secret}/read")

    # First story ships content:encoded; Second story only a two-word summary.
    assert re.search(
        r'First story</a>\s*<span class="row-sub">Example Feed(?: · \d+ \w+ \d{4})? · \d+ minute',
        page.text,
    )
    assert re.search(
        r'Second story</a>\s*<span class="row-sub">Example Feed(?: · \d+ \w+ \d{4})?</span>', page.text
    )


def test_feeds_page_lists_bluesky_as_a_curator_source(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "b1",
                    "url": "https://bsky.app/profile/alice.example/post/3k1",
                    "title": "A Bluesky post",
                    "source": "@alice.example",
                    "content": "<p>Post body</p>",
                }
            ]
        },
    )

    page = client.get(f"/{secret}/feeds")
    assert "Curator sources" in page.text
    assert "1 post from 1 account" in page.text
    assert "source=Bluesky" in page.text


def test_a_subscriber_cookie_is_encrypted_and_editable(client, tmp_path):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    feed_id = client.app.state.matins.list_feeds(secret)[0].id
    csrf = _csrf(client, secret)

    saved = client.post(
        f"/{secret}/feeds/{feed_id}/auth",
        data={"csrf": csrf, "cookie": "lm_session=abc123"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert client.app.state.matins.get_feed(secret, feed_id).auth_cookie == "lm_session=abc123"
    assert b"lm_session=abc123" not in (tmp_path / "app.db").read_bytes()
    assert "A cookie is set" in client.get(f"/{secret}/feeds/{feed_id}").text

    client.post(
        f"/{secret}/feeds/{feed_id}/auth",
        data={"csrf": csrf, "cookie": "-"},
        follow_redirects=False,
    )
    assert client.app.state.matins.get_feed(secret, feed_id).auth_cookie is None


def test_the_feed_cookie_reaches_the_article_fetch(client, monkeypatch):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    feed_id = client.app.state.matins.list_feeds(secret)[0].id
    client.app.state.matins.set_feed_auth(secret, feed_id, "lm_session=abc123")
    article_id = _reader_article_id(client, secret, "Second story")

    seen: list[str | None] = []

    async def fake(_self, _url, *, cookie=None):
        seen.append(cookie)
        return None

    monkeypatch.setattr("later_ink.matins.rss.RssConnector._fetch_page", fake)
    client.get(f"/{secret}/read", params={"view": "all", "article": article_id})

    assert seen == ["lm_session=abc123"]


def test_a_cookie_does_not_follow_a_cross_site_redirect(monkeypatch):
    from later_ink import fetch

    async def noop(_url):
        return None

    monkeypatch.setattr(fetch, "_validate", noop)
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.host == "www.lemonde.fr":
            return httpx.Response(302, headers={"location": "https://auth.lemonde.fr/x"})
        if request.url.host == "auth.lemonde.fr":
            return httpx.Response(302, headers={"location": "https://evil.example.net/x"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        fetch.fetch_bytes(
            client,
            "https://www.lemonde.fr/a",
            timeout=1,
            max_bytes=100,
            headers={"Cookie": "secret"},
        )
    )

    assert result is not None
    assert seen == [
        ("www.lemonde.fr", "secret"),
        ("auth.lemonde.fr", "secret"),
        ("evil.example.net", None),
    ]


def test_reader_scroll_restorer_is_allowed_by_the_csp(client):
    import base64
    import hashlib

    from later_ink.matins import pages as matins_pages

    secret = _signup_without_readwise(client)
    page = client.get(f"/{secret}/read")
    digest = base64.b64encode(
        hashlib.sha256(matins_pages.READER_SCROLL_JS.encode("utf-8")).digest()
    ).decode()

    assert f"<script>{matins_pages.READER_SCROLL_JS}</script>" in page.text
    assert f"script-src 'sha256-{digest}'" in page.headers["content-security-policy"]


def test_crosspoint_gets_relative_issue_acquisition_links(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/chosen"})
    client.post(
        f"/{secret}/issues/build",
        data={"csrf": _csrf(client, secret)},
        follow_redirects=False,
    )
    [issue] = client.app.state.matins.list_issues(secret)

    crosspoint = client.get(
        f"/{secret}/issues/",
        headers={
            "Accept": "application/atom+xml",
            "User-Agent": "CrossPoint-ESP32-1.0.19",
        },
    )
    hrefs = re.findall(
        r'rel="http://opds-spec.org/acquisition" href="([^"]+)"',
        crosspoint.text,
    )
    assert hrefs == [f"{issue.id}.epub"]

    koreader = client.get(
        f"/{secret}/issues/",
        headers={"Accept": "application/atom+xml", "User-Agent": "KOReader/2024"},
    )
    absolute = re.findall(
        r'rel="http://opds-spec.org/acquisition" href="([^"]+)"',
        koreader.text,
    )
    assert absolute and absolute[0].startswith("http")
    assert absolute[0].endswith(f"/issues/{issue.id}.epub")


def test_an_issue_can_be_browsed_in_the_reader(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/chosen"})
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/issues/build",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    [issue] = client.app.state.matins.list_issues(secret)

    detail = client.get(f"/{secret}/issues/{issue.id}")
    assert "See in reader" in detail.text
    assert f"/read?issue={issue.id}" in detail.text

    reader = client.get(f"/{secret}/read", params={"issue": issue.id})
    assert "Issue ·" in reader.text
    # The saved link was consumed by the issue and is gone from the live queue,
    # but the edition still remembers it.
    assert "Chosen" in reader.text
    assert "First story" in reader.text


def test_reader_filters_by_source(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "b1",
                    "url": "https://bsky.app/profile/alice.example/post/3k1",
                    "title": "A Bluesky post",
                    "source": "@alice.example",
                    "content": "<p>Post body</p>",
                }
            ]
        },
    )

    bluesky = client.get(f"/{secret}/read", params={"source": "Bluesky"})
    assert "A Bluesky post" in bluesky.text
    assert "First story" not in bluesky.text
    assert 'class="source-filter"' in bluesky.text
    # Authors are not separate filter entries; Bluesky is one source.
    assert bluesky.text.count(">Bluesky</button>") == 1
    assert "@alice.example</button>" not in bluesky.text

    feed = client.get(f"/{secret}/read", params={"source": "Example Feed"})
    assert "First story" in feed.text
    assert "A Bluesky post" not in feed.text


def test_archived_bluesky_posts_stay_under_the_bluesky_source(client):
    secret = _signup_without_readwise(client)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "b1",
                    "url": "https://bsky.app/profile/alice.example/post/3k1",
                    "title": "A Bluesky post",
                    "source": "@alice.example",
                    "content": "<p>Post body</p>",
                }
            ]
        },
    )
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/read/archive",
        data={"csrf": csrf, "article_id": "cand:b1", "view": "all"},
        follow_redirects=False,
    )

    archived = client.get(
        f"/{secret}/read", params={"view": "archived", "source": "Bluesky"}
    )
    assert "A Bluesky post" in archived.text
    assert "Bluesky · @alice.example" in archived.text


def test_reading_and_opening_count_as_interest(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article_id = _reader_article_id(client, secret, "First story")

    client.get(f"/{secret}/read", params={"view": "all", "article": article_id})
    signals = client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    assert [(s["url"], s["kind"], s["weight"]) for s in signals] == [
        ("https://example.com/first", "read", 0.35)
    ]

    opened = client.get(
        f"/{secret}/read/open",
        params={"article": article_id},
        follow_redirects=False,
    )
    assert opened.status_code == 302
    assert opened.headers["location"] == "https://example.com/first"

    signals = client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    assert [(s["kind"], s["weight"]) for s in signals] == [("opened", 0.7)]

    # An explicit like still wins over the implicit events.
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/read/feedback",
        data={"csrf": csrf, "article_id": article_id, "value": "like", "view": "all"},
        follow_redirects=False,
    )
    signals = client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    assert [(s["kind"], s["weight"]) for s in signals] == [("like", 1.0)]


def test_reader_archive_hides_signals_unqueues_and_restores(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article_id = _reader_article_id(client, secret, "First story")
    csrf = _csrf(client, secret)

    client.post(
        f"/{secret}/read/queue",
        data={"csrf": csrf, "article_id": article_id, "view": "all"},
        follow_redirects=False,
    )
    assert client.app.state.matins.list_saved(secret)

    archived = client.post(
        f"/{secret}/read/archive",
        data={"csrf": csrf, "article_id": article_id, "view": "all"},
        follow_redirects=False,
    )
    assert archived.status_code == 303
    assert "First story" not in client.get(f"/{secret}/read?view=all").text
    assert "First story" in client.get(f"/{secret}/read?view=archived").text
    assert client.app.state.matins.list_saved(secret) == []
    signals = client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    assert [(s["url"], s["rating"]) for s in signals] == [
        ("https://example.com/first", "not_interested")
    ]

    [row] = client.app.state.matins.list_archived(secret)
    restored = client.post(
        f"/{secret}/read/restore",
        data={"csrf": csrf, "article_id": row.id, "view": "archived"},
        follow_redirects=False,
    )
    assert restored.status_code == 303
    assert "First story" in client.get(f"/{secret}/read?view=all").text
    assert "First story" not in client.get(f"/{secret}/read?view=archived").text
    assert client.get("/api/v1/signals", headers=_api(secret)).json()["signals"] == []


def test_a_curated_catalogue_prints_only_the_queue(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/chosen"})

    page = client.get(f"/{secret}/settings")
    assert 'name="auto_select"' in page.text
    client.post(
        f"/{secret}/settings",
        data={"csrf": _csrf(client, secret), "auto_select": "0"},
        follow_redirects=False,
    )

    preview = client.get(f"/{secret}/issues/preview")
    assert "https://example.com/chosen" in preview.text or "chosen" in preview.text.lower()
    assert "First story" not in preview.text

    composed = client.post(
        f"/{secret}/issues/build",
        data={"csrf": _csrf(client, secret)},
        follow_redirects=False,
    )
    assert composed.status_code == 303
    issues = client.app.state.matins.list_issues(secret)
    assert len(issues) == 1
    assert issues[0].article_count == 1


def test_candidates_are_browsable_and_queue_with_their_text(client):
    secret = _signup_without_readwise(client)
    created = client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "b1",
                    "url": "https://bsky.app/profile/alice.example/post/3k1",
                    "title": "A Bluesky post",
                    "source": "@alice.example",
                    "summary": "Short text",
                    "content": "<p>Post body</p>",
                    "published": datetime(2026, 10, 30, 8, 0, tzinfo=UTC).timestamp(),
                }
            ]
        },
    )
    assert created.status_code == 201
    assert created.json()["added"] == 1

    page = client.get(f"/{secret}/read")
    assert "A Bluesky post" in page.text
    assert "Bluesky · @alice.example" in page.text

    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/read/queue",
        data={"csrf": csrf, "article_id": "cand:b1", "view": "queued"},
        follow_redirects=False,
    )
    saved = [
        item
        for item in client.app.state.matins.list_saved(secret)
        if item.url == "https://bsky.app/profile/alice.example/post/3k1"
    ]
    assert saved
    assert saved[0].content == "<p>Post body</p>"


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


# ------------------------------------------------------------------- the app


HTML = {"accept": "text/html"}


def test_every_page_shares_the_navigation(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    feed = client.app.state.matins.list_feeds(secret)[0]
    for path in ("", "feeds", f"feeds/{feed.id}", "issues/preview", "issues/", "settings", "read"):
        page = client.get(f"/{secret}/{path}", headers=HTML)
        assert page.status_code == 200, path
        for target in ("issues/", "read", "settings"):
            assert f'href="/{secret}/{target}"' in page.text, (path, target)
        assert page.text.count("aria-current=page") == 1, path


def test_issues_list_is_html_for_people_and_opds_for_readers(client):
    secret = _signup_without_readwise(client)
    assert "<h1>Issues</h1>" in client.get(f"/{secret}/issues/", headers=HTML).text
    assert "<feed" in client.get(f"/{secret}/issues/", headers={"accept": "*/*"}).text


def test_a_first_feed_turns_on_the_daily_issue(client):
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    assert store.get_delivery(secret) is None
    _add_feed(client, secret)
    delivery = store.get_delivery(secret)
    assert delivery is not None and delivery.enabled
    assert secret in {d.secret for d in store.list_deliveries()}

    # An explicit "off" is not overridden by adding another feed.
    csrf = _csrf(client, secret)
    client.post(f"/{secret}/schedule", data={"csrf": csrf, "enabled": "0"})
    _add_feed(client, secret, "https://example.com/other")
    assert not store.get_delivery(secret).enabled


def test_signing_up_by_email_schedules_issues_and_notifies_that_address(client):
    client.post("/signin", data={"email": "ivo@example.com"})
    landed = client.post(
        "/signin/code",
        data={"email": "ivo@example.com", "code": client.sent_codes["ivo@example.com"]},
        follow_redirects=False,
    )
    secret = landed.headers["location"].strip("/")
    delivery = client.app.state.matins.get_delivery(secret)
    assert delivery.enabled and delivery.email == "ivo@example.com"
    assert 'value="ivo@example.com"' in client.get(f"/{secret}/settings").text


def test_clearing_the_email_removes_sign_in(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    for email in ("ivo@example.com", ""):
        client.post(
            f"/{secret}/settings",
            data={"csrf": csrf, "email": email, "schedule": "07:00", "enabled": "1"},
        )
    assert client.app.state.matins.email_for_secret(secret) is None


def test_skipping_takes_an_article_out_and_putting_back_restores_it(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    preview = client.get(f"/{secret}/issues/preview").text
    article = re.search(r'name="article" value="(feeds:[^"]+)"', preview).group(1)
    title = re.search(r'name="title" value="([^"]+)"', preview).group(1)

    done = client.post(
        f"/{secret}/issues/skip",
        data={"csrf": csrf, "article": article, "title": title, "section": "Example Feed"},
    )
    assert "Skipped" in done.text
    assert f'name="title" value="{title}"' not in done.text
    assert "Put back" in done.text

    client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    store = client.app.state.matins
    with store._conn() as conn:
        printed = {r["article_id"] for r in conn.execute("select article_id from issue_items")}
    assert article not in printed

    client.post(f"/{secret}/issues/skip/undo", data={"csrf": csrf, "article": article})
    assert store.skipped_ids(secret) == set()


def test_skip_needs_csrf_and_a_feed_article(client):
    secret = _signup_without_readwise(client)
    assert client.post(f"/{secret}/issues/skip", data={"article": "feeds:x"}).status_code == 403
    csrf = _csrf(client, secret)
    bad = client.post(f"/{secret}/issues/skip", data={"csrf": csrf, "article": "saved:1"})
    assert bad.status_code == 400


def test_a_paused_feed_stays_out_of_the_issue(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    feed = client.app.state.matins.list_feeds(secret)[0]
    client.post(f"/{secret}/feeds/{feed.id}/pause", data={"csrf": csrf, "paused": "1"})
    assert "Nothing is queued" in client.get(f"/{secret}/issues/preview").text
    client.post(f"/{secret}/feeds/{feed.id}/pause", data={"csrf": csrf, "paused": "0"})
    assert "First story" in client.get(f"/{secret}/issues/preview").text


def test_limits_in_settings_shape_the_issue_and_are_validated(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    form = {"csrf": csrf, "schedule": "07:00", "timezone": "Europe/Paris", "enabled": "1"}

    bad = client.post(f"/{secret}/settings", data={**form, "max_articles": "5000"})
    assert bad.status_code == 400
    assert "between 1 and 100" in bad.text
    assert 'value="5000"' in bad.text

    client.post(f"/{secret}/settings", data={**form, "max_per_feed": "1"})
    assert client.app.state.matins.get_delivery(secret).max_per_feed == 1
    preview = client.get(f"/{secret}/issues/preview").text
    assert preview.count('name="article"') == 1


def test_feed_page_explains_each_article(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    feed = client.app.state.matins.list_feeds(secret)[0]
    assert client.get(f"/{secret}/feeds/nope").status_code == 404

    client.post(f"/{secret}/feeds/{feed.id}/rename", data={"csrf": csrf, "title": "My name"})
    page = client.get(f"/{secret}/feeds/{feed.id}").text
    assert "My name" in page
    assert "First story" in page
    assert "In next issue" in page or "Too old" in page
    assert "OK" in page


def test_feed_failures_are_recorded_and_shown(client, monkeypatch):
    from later_ink.connectors.base import UpstreamError

    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)

    async def down(url, *, timeout, max_bytes):
        raise UpstreamError("Could not fetch that feed", 502)

    monkeypatch.setattr("later_ink.matins.rss.fetch_feed", down)
    client.post(f"/{secret}/feeds/refresh", data={"csrf": csrf})
    feed = client.app.state.matins.list_feeds(secret)[0]
    assert feed.last_error == "Could not fetch that feed"
    assert "Failing" in client.get(f"/{secret}/feeds").text
    assert "1 feed failing" in client.get(f"/{secret}/issues/", headers=HTML).text


def test_a_site_address_subscribes_to_the_feed_it_advertises(client, monkeypatch):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    site = (
        b'<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml">'
        b"</head><body>Hi</body></html>"
    )

    async def fetch(url, *, timeout, max_bytes):
        return RSS_XML if url.endswith("/rss.xml") else site

    monkeypatch.setattr("later_ink.matins.rss.fetch_feed", fetch)
    client.post(f"/{secret}/feeds", data={"csrf": csrf, "url": "https://example.com/"})
    feeds = client.app.state.matins.list_feeds(secret)
    assert [feed.url for feed in feeds] == ["https://example.com/rss.xml"]
    assert feeds[0].title == "Example Feed"


def test_opml_round_trip(client):
    secret = _signup_without_readwise(client)
    csrf = _csrf(client, secret)
    opml = b"""<?xml version="1.0"?><opml version="2.0"><body>
      <outline text="Folder">
        <outline text="One &amp; only" xmlUrl="https://one.example/feed"/>
        <outline text="Bad" xmlUrl="javascript:alert(1)"/>
      </outline>
      <outline title="Two" xmlUrl="https://two.example/feed"/>
    </body></opml>"""
    done = client.post(
        f"/{secret}/feeds/import",
        data={"csrf": csrf},
        files={"file": ("subs.opml", opml, "text/xml")},
    )
    assert "Imported 2 feeds" in done.text
    assert client.app.state.matins.get_delivery(secret).enabled

    exported = client.get(f"/{secret}/feeds.opml")
    assert exported.headers["content-type"].startswith("text/x-opml")
    assert 'text="One &amp; only"' in exported.text
    assert "javascript" not in exported.text

    again = client.post(
        f"/{secret}/feeds/import",
        data={"csrf": csrf},
        files={"file": ("subs.opml", exported.content, "text/xml")},
    )
    assert "Nothing new" in again.text

    junk = client.post(
        f"/{secret}/feeds/import",
        data={"csrf": csrf},
        files={"file": ("subs.opml", b"<html></html>", "text/xml")},
    )
    assert "not valid OPML" in junk.text


def test_issue_page_lists_contents_and_deleting_releases_the_articles(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    csrf = _csrf(client, secret)
    client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    store = client.app.state.matins
    issue = store.list_issues(secret)[0]
    assert store.list_saved(secret) == []

    page = client.get(f"/{secret}/issues/{issue.id}", headers=HTML)
    assert "First story" in page.text
    assert f"/{issue.id}.epub" in page.text
    home = client.get(f"/{secret}/issues/", headers=HTML).text
    assert f"/issues/{issue.id}.epub" in home

    from pathlib import Path

    client.post(f"/{secret}/issues/{issue.id}/delete", data={"csrf": csrf})
    assert store.list_issues(secret) == []
    assert not Path(issue.path).exists()
    assert len(store.list_saved(secret)) == 1
    assert "First story" in client.get(f"/{secret}/issues/preview").text


def test_a_slow_compose_hands_over_to_a_polling_page(client, monkeypatch):
    import asyncio

    from later_ink.matins import routes

    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    gate: dict = {}
    real = routes._compose

    async def slow(app, target):
        gate["event"] = asyncio.Event()
        await gate["event"].wait()
        return await real(app, target)

    monkeypatch.setattr(routes, "_compose", slow)
    monkeypatch.setattr(routes, "COMPOSE_WAIT", 0.01)

    started = client.post(
        f"/{secret}/issues/build", data={"csrf": csrf}, follow_redirects=False
    )
    assert started.headers["location"] == f"/{secret}/issues/composing"
    waiting = client.get(f"/{secret}/issues/composing", follow_redirects=False)
    assert waiting.status_code == 200
    assert 'http-equiv="refresh"' in waiting.text

    client.portal.call(gate["event"].set)
    routes_task = routes._composing[secret]
    client.portal.call(asyncio.wait, {routes_task})
    done = client.get(f"/{secret}/issues/composing", follow_redirects=False)
    assert done.status_code == 303
    assert "/issues/" in done.headers["location"]
    assert "Composed" in client.get(done.headers["location"]).text


def test_rotating_the_address_keeps_the_catalogue(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    client.post(
        f"/{secret}/settings",
        data={"csrf": csrf, "email": "ivo@example.com", "schedule": "06:30", "enabled": "1"},
    )
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    client.post(f"/{secret}/issues/build", data={"csrf": csrf})
    store = client.app.state.matins
    old_issue = store.list_issues(secret)[0]

    rotated = client.post(f"/{secret}/regenerate", data={"csrf": csrf}, follow_redirects=False)
    assert rotated.status_code == 303
    new = rotated.headers["location"].split("/")[1]
    assert new != secret

    assert client.get(f"/{secret}/").status_code == 404
    assert [f.url for f in store.list_feeds(new)] == ["https://example.com/feed"]
    assert store.get_delivery(new).schedule == "06:30"
    assert store.secret_for_email("ivo@example.com") == new
    download = client.get(f"/{new}/issues/{old_issue.id}.epub")
    assert download.status_code == 200 and download.content[:2] == b"PK"
    # Nothing printed before the rotation is printed again after it.
    assert "Nothing is queued" in client.get(f"/{new}/issues/preview").text
    assert store.list_feeds(secret) == []
    landing = client.get("/").text
    assert new in landing and secret not in landing


def test_browsers_get_a_page_for_errors_and_the_api_keeps_json(client):
    missing = client.get("/not-a-catalogue-at-all/", headers=HTML)
    assert missing.status_code == 404
    assert "Sign in" in missing.text
    assert client.get("/not-a-catalogue-at-all/").headers["content-type"].startswith(
        "application/json"
    )


def test_next_run_says_when_in_words():
    from later_ink.matins.issues import next_run

    early = datetime(2026, 10, 30, 4, 0, tzinfo=UTC)
    late = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
    assert next_run("07:00", "Europe/Paris", issued_today=False, now=early) == "Today at 07:00"
    assert next_run("07:00", "Europe/Paris", issued_today=True, now=early) == "Tomorrow at 07:00"
    assert next_run("07:00", "Europe/Paris", issued_today=False, now=late).startswith("Due now")


def test_user_text_is_escaped_in_feed_pages(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    feed = client.app.state.matins.list_feeds(secret)[0]
    client.post(
        f"/{secret}/feeds/{feed.id}/rename", data={"csrf": csrf, "title": "<script>x</script>"}
    )
    for path in ("feeds", f"feeds/{feed.id}", "issues/preview"):
        assert "<script>x" not in client.get(f"/{secret}/{path}").text
    flash = client.get(f"/{secret}/feeds?notice=%3Cimg%20src%3Dx%3E").text
    assert "<img src=x>" not in flash


# ----------------------------------------------------------------------- API


def _api(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def test_api_requires_a_bearer_secret(client):
    secret = _signup_without_readwise(client)
    assert client.get("/api/v1/items").status_code == 401
    assert client.get("/api/v1/items", headers={"authorization": "Bearer nope"}).status_code == 404
    assert client.get("/api/v1/items", headers=_api(secret)).status_code == 200


def test_api_queues_items_with_and_without_content(client):
    secret = _signup_without_readwise(client)
    created = client.post(
        "/api/v1/items",
        headers=_api(secret),
        json={"url": "https://example.com/a", "title": "From an agent"},
    )
    assert created.status_code == 201
    assert created.json()["queued"] is True

    duplicate = client.post(
        "/api/v1/items", headers=_api(secret), json={"url": "https://example.com/a"}
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["queued"] is False

    with_content = client.post(
        "/api/v1/items",
        headers=_api(secret),
        json={
            "url": "https://example.com/paywalled",
            "title": "Supplied body",
            "content": "<p>Text the server could never have fetched.</p>",
        },
    )
    assert with_content.status_code == 201

    listed = client.get("/api/v1/items", headers=_api(secret))
    items = listed.json()["items"]
    assert len(items) == 2
    assert any(item["has_content"] for item in items)

    # The supplied body is used instead of fetching the page.
    saved = [i for i in client.app.state.matins.list_saved(secret) if i.content]
    assert saved
    from later_ink.matins.queue import QueueConnector

    connector = QueueConnector(client.app.state.matins, secret)
    article, html = __import__("asyncio").run(
        connector.get_article_html(str(saved[0].id))
    )
    assert "Text the server could never have fetched" in html


def test_api_rejects_a_bad_item_and_a_huge_body(client):
    secret = _signup_without_readwise(client)
    assert (
        client.post("/api/v1/items", headers=_api(secret), json={"url": "nope"}).status_code
        == 400
    )
    too_big = client.post(
        "/api/v1/items",
        headers=_api(secret),
        json={"url": "https://example.com/x", "content": "x" * (600 * 1024)},
    )
    assert too_big.status_code == 413


def test_api_manages_feeds(client):
    secret = _signup_without_readwise(client)
    added = client.post(
        "/api/v1/feeds", headers=_api(secret), json={"url": "https://example.com/feed"}
    )
    assert added.status_code == 201
    assert added.json()["title"] == "Example Feed"

    listed = client.get("/api/v1/feeds", headers=_api(secret)).json()["feeds"]
    assert len(listed) == 1

    assert (
        client.delete(
            f"/api/v1/feeds/{listed[0]['id']}", headers=_api(secret)
        ).status_code
        == 204
    )
    assert client.get("/api/v1/feeds", headers=_api(secret)).json()["feeds"] == []


def test_api_previews_and_composes(client):
    secret = _signup_without_readwise(client)
    client.post("/api/v1/feeds", headers=_api(secret), json={"url": "https://example.com/feed"})
    client.post(
        "/api/v1/items", headers=_api(secret), json={"url": "https://example.com/saved"}
    )

    preview = client.get("/api/v1/issues/preview", headers=_api(secret)).json()
    assert len(preview["articles"]) >= 2

    first = client.post("/api/v1/issues", headers=_api(secret))
    assert first.status_code == 201
    assert first.json()["articles"] >= 2

    again = client.post("/api/v1/issues", headers=_api(secret))
    assert again.status_code == 409
    forced = client.post("/api/v1/issues?force=1", headers=_api(secret))
    assert forced.status_code in (200, 201)


def test_api_queue_only_issue_is_exactly_what_an_agent_queued(client):
    """A curator queues its picks and asks for those alone: the automatic feed
    selection must not dilute the edition it just chose."""
    secret = _signup_without_readwise(client)
    client.post("/api/v1/feeds", headers=_api(secret), json={"url": "https://example.com/feed"})
    client.post(
        "/api/v1/items", headers=_api(secret), json={"url": "https://example.com/pick"}
    )

    everything = client.get("/api/v1/issues/preview", headers=_api(secret)).json()
    assert {a["url"] for a in everything["articles"]} == {
        "https://example.com/pick",
        "https://example.com/first",
        "https://example.com/second",
    }

    queue_only = client.get(
        "/api/v1/issues/preview?only=queue", headers=_api(secret)
    ).json()
    assert [a["url"] for a in queue_only["articles"]] == ["https://example.com/pick"]

    composed = client.post("/api/v1/issues?only=queue", headers=_api(secret))
    assert composed.status_code == 201
    assert composed.json()["articles"] == 1

    # The parameter is deliberately narrow: "everything" is its absence.
    bad = client.get("/api/v1/issues/preview?only=feeds", headers=_api(secret))
    assert bad.status_code == 400
    assert client.post("/api/v1/issues?only=nope", headers=_api(secret)).status_code == 400



# ------------------------------------------------- queue priority and quality


def _prose(words: int = 300) -> str:
    return "<html><body><article><h1>T</h1>" + "".join(
        f"<p>{'word ' * 60}sentence, with enough text to count as a paragraph.</p>"
        for _ in range(words // 60)
    ) + "</article></body></html>"


def _serve_pages(monkeypatch, pages=None):
    async def fake(url):
        # No archive copy unless the test puts one in `pages`: otherwise every
        # failed fetch would "succeed" through the Wayback fallback.
        if url.startswith("https://web.archive.org/"):
            return (pages or {}).get(url)
        return (pages or {}).get(url, _prose())

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)


def test_user_picks_print_before_curator_picks_and_are_never_crowded_out(client, monkeypatch):
    _serve_pages(monkeypatch)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)
    store.set_issue_limits(
        secret, max_articles=None, max_per_feed=None, max_words=None, window_hours=None, max_saved=3
    )
    for n in range(2):
        store.save_url(secret, f"https://u.example/{n}", f"User {n}")
    for n in range(4):
        store.save_url(secret, f"https://c.example/{n}", f"Curator {n}", origin="curator")

    client.post(f"/{secret}/issues/build", data={"csrf": _csrf(client, secret)})
    left = {item.url for item in store.list_saved(secret)}
    assert not {f"https://u.example/{n}" for n in range(2)} & left
    # Newest curator pick took the one slot left; three wait.
    assert "https://c.example/3" not in left
    assert len(left) == 3


def test_a_failed_fetch_does_not_cost_a_slot(client, monkeypatch):
    async def fake(url):
        return None if "dead" in url else _prose()

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)
    store.set_issue_limits(
        secret, max_articles=None, max_per_feed=None, max_words=None, window_hours=None, max_saved=1
    )
    store.save_url(secret, "https://x.example/dead", "Dead")
    store.save_url(secret, "https://x.example/alive", "Alive")
    client.post(f"/{secret}/issues/build", data={"csrf": _csrf(client, secret)})
    assert [item.title for item in store.list_saved(secret)] == ["Dead"]


def test_the_real_word_count_decides_what_fits(client, monkeypatch):
    _serve_pages(monkeypatch)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)
    store.set_issue_limits(
        secret, max_articles=None, max_per_feed=None, max_words=500, window_hours=None, max_saved=None
    )
    for n in range(3):
        store.save_url(secret, f"https://x.example/{n}", f"N{n}")
    client.post(f"/{secret}/issues/build", data={"csrf": _csrf(client, secret)})
    assert store.list_issues(secret)[0].word_count <= 500 or store.list_issues(secret)[0].article_count == 1
    assert store.list_saved(secret)


def test_a_blocked_queue_item_prints_the_lede_not_the_wall(client, monkeypatch):
    wall = (
        "<html><body><h1>Client Challenge</h1><p>JavaScript is disabled in your browser. "
        "A required part of this site couldn\u2019t load. " + "Please check your connection. " * 12 +
        "</p></body></html>"
    )
    _serve_pages(monkeypatch, {"https://lemonde.example/a": wall, "https://lemonde.example/b": wall})
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    # A lede worth printing: a real sentence, not the "Comments" Hacker News
    # ships as its description.
    store.save_url(
        secret,
        "https://lemonde.example/a",
        "A",
        lede="The real lede, the one the publisher put above the link on its own page.",
    )
    store.save_url(secret, "https://lemonde.example/b", "B")
    reader = asyncio.run(main._tenant_connector(secret))
    queue = reader.sources["later"]
    items = {i.title: i for i in store.list_saved(secret)}

    _, html = asyncio.run(queue.get_article_html(str(items["A"].id)))
    assert "The real lede" in html and "required part" not in html

    from later_ink.connectors.base import ArticleUnavailable

    with pytest.raises(ArticleUnavailable):
        asyncio.run(queue.get_article_html(str(items["B"].id)))


def test_a_long_article_quoting_a_block_phrase_is_not_a_wall():
    from later_ink.matins.extract import looks_blocked

    assert not looks_blocked("<p>" + "word " * 500 + "access denied</p>")
    assert looks_blocked("<p>Access denied</p>")


def test_is_stub_tells_a_pointer_from_a_short_article():
    from later_ink.matins.extract import is_stub

    # Hacker News' description of every story is one link labelled "Comments".
    assert is_stub('<p><a href="https://news.ycombinator.com/item?id=1">Comments</a></p>')
    # A teaser that only says where the article is.
    assert is_stub('<p><a href="https://lemonde.fr/a">Read the full article on Le Monde</a></p>')
    assert is_stub("")
    assert is_stub("   ")
    # Short is not the same as empty: a brief prints.
    assert not is_stub("<p>Two people died in a crash on the M20 this morning near Kent.</p>")
    # An image with no words is still a post.
    assert not is_stub('<figure><img src="/comic.jpg" alt="a comic"/></figure>')
    assert not is_stub(ARTICLE_HTML)


def test_a_queued_link_whose_lede_is_a_comments_label_is_not_printed(client, monkeypatch):
    """The 8 November issue printed a page whose whole body was "Comments"."""
    _serve_pages(monkeypatch, {"https://science.example/brain": None})
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)
    store.save_url(secret, "https://x.example/good", "A real one")
    store.save_url(secret, "https://science.example/brain", "Brain data", lede="Comments")

    payload = client.post("/api/v1/issues?only=queue", headers=_api(secret)).json()

    assert payload["articles"] == 1
    assert [row["title"] for row in payload["skipped"]] == ["Brain data"]
    assert "no text to print" in payload["skipped"][0]["reason"]
    # Dropped for good: out of the queue, and out of the curator's shortlist.
    assert [item.title for item in store.list_saved(secret)] == []
    assert store.archived_urls(secret, ["https://science.example/brain"]) == {
        "https://science.example/brain"
    }


def test_a_curator_recap_prints_on_its_own_when_there_is_no_body(client, monkeypatch):
    _serve_pages(monkeypatch, {"https://science.example/brain": None})
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.save_url(secret, "https://science.example/brain", "Brain data", lede="Comments")
    item = store.list_saved(secret)[0]
    assert client.put(
        f"/api/v1/items/{item.id}/recap",
        json={"recap": "Headset makers sell brain data to AI trainers."},
        headers=_api(secret),
    ).status_code == 204

    reader = asyncio.run(main._tenant_connector(secret))
    _article, html = asyncio.run(reader.sources["later"].get_article_html(str(item.id)))

    assert "Headset makers sell brain data to AI trainers." in html
    assert "Recap" in html
    # No body under the recap, so no rule pointing at nothing.
    assert "<hr/>" not in html


def test_the_archive_copy_stands_in_when_the_publisher_refuses(client, monkeypatch):
    url = "https://science.example/brain"
    _serve_pages(
        monkeypatch,
        {
            # What a datacenter IP gets from Cloudflare: a challenge, not a page.
            url: "<html><body><h1>Just a moment...</h1></body></html>",
            f"https://web.archive.org/web/2id_/{url}": _prose(),
        },
    )
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.save_url(secret, url, "Brain data")
    reader = asyncio.run(main._tenant_connector(secret))
    item = store.list_saved(secret)[0]

    _article, html = asyncio.run(reader.sources["later"].get_article_html(str(item.id)))

    assert "enough text to count as a paragraph" in html
    assert "Just a moment" not in html


def test_a_feed_item_that_is_only_a_link_to_itself_is_not_printed(client, monkeypatch):
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Hacker News</title>
  <item>
    <title>Consumer brain wave headsets threaten privacy</title>
    <link>https://science.example/brain</link>
    <guid>https://science.example/brain</guid>
    <pubDate>Fri, 30 Oct 2026 06:00:00 GMT</pubDate>
    <description>&lt;a href="https://news.ycombinator.com/item?id=1"&gt;Comments&lt;/a&gt;</description>
  </item>
</channel></rss>
"""

    async def feed(_url, *, timeout, max_bytes):
        return xml

    monkeypatch.setattr("later_ink.matins.rss.fetch_feed", feed)
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)

    async def unreachable(_self, _url, *, cookie=None):
        return None

    monkeypatch.setattr("later_ink.matins.rss.RssConnector._fetch_page", unreachable)

    payload = client.post("/api/v1/issues", headers=_api(secret)).json()

    # Nothing to print, so no issue at all — and the story is not offered again.
    assert payload["issue"] is None
    assert [row["title"] for row in payload["skipped"]] == [
        "Consumer brain wave headsets threaten privacy"
    ]
    assert "no text to print" in payload["skipped"][0]["reason"]
    assert "https://science.example/brain" in store.archived_urls(
        secret, ["https://science.example/brain"]
    )


def test_a_forum_page_prints_the_post_and_the_first_replies_only():
    reply = "This is a considered reply with quite a few words in it, enough to count here. "
    rows = "".join(
        f'<table><tr><td class="default"><div class="comment"><div class="commtext">{reply}{n}</div></div></td></tr></table>'
        for n in range(40)
    )
    raw = (
        '<html><body><table><tr><td><div class="toptext">'
        + "<p>Which television respects my privacy? " + "I would like one without tracking. " * 8 + "</p>"
        + f"</div></td></tr></table>{rows}</body></html>"
    )
    html = html_to_content(raw, "https://forum.example/1")
    assert "From the discussion" in html
    assert html.count("<blockquote>") == 8
    assert "32 more replies" in html
    assert "<table" not in html


def test_recaps_are_listed_for_long_items_and_printed_first(client, monkeypatch):
    _serve_pages(monkeypatch, {"https://x.example/long": _prose(6000)})
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.save_url(secret, "https://x.example/short", "Short")
    store.save_url(secret, "https://x.example/long", "Long")

    listed = client.get("/api/v1/items/long", headers=_api(secret)).json()["items"]
    assert [row["title"] for row in listed] == ["Long"]

    item = listed[0]["id"]
    assert client.put(
        f"/api/v1/items/{item}/recap", json={"recap": "In <short>."}, headers=_api(secret)
    ).status_code == 204
    assert client.get("/api/v1/items/long", headers=_api(secret)).json()["items"] == []
    reader = asyncio.run(main._tenant_connector(secret))
    _, html = asyncio.run(reader.sources["later"].get_article_html(str(item)))
    assert html.startswith("<blockquote><p><strong>Recap</strong>")
    assert "In &lt;short&gt;." in html


def test_api_marks_curator_picks_and_a_user_pick_outranks_them(client):
    secret = _signup_without_readwise(client)
    body = {"url": "https://x.example/a", "origin": "curator", "lede": "L"}
    assert client.post("/api/v1/items", json=body, headers=_api(secret)).status_code == 201
    store = client.app.state.matins
    assert store.list_saved(secret)[0].origin == "curator"
    store.save_url(secret, "https://x.example/a")
    assert store.list_saved(secret)[0].origin == "user"
    client.post("/api/v1/items", json=body, headers=_api(secret))
    assert store.list_saved(secret)[0].origin == "user"
    assert client.post(
        "/api/v1/items", json={"url": "https://x.example/b", "origin": "x"}, headers=_api(secret)
    ).status_code == 400


def test_queued_status_includes_items_read_in_the_reader(client):
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.save_url(secret, "https://x.example/a", "A")
    item = store.list_saved(secret)[0]
    store.mark_read(secret, f"saved:{item.id}")
    rows = client.get("/api/v1/items?status=queued", headers=_api(secret)).json()["items"]
    assert [row["url"] for row in rows] == ["https://x.example/a"]


def test_signed_in_home_goes_to_the_app(client):
    secret = _signup_without_readwise(client)
    client.get(f"/{secret}/", headers=HTML)
    landing = client.get("/", headers=HTML, follow_redirects=False)
    assert landing.status_code == 303 and landing.headers["location"] == f"/{secret}/"
    assert "Clone me" in client.get("/?about=1", headers=HTML).text

    _add_feed(client, secret)
    home = client.get(f"/{secret}/", headers=HTML, follow_redirects=False)
    assert home.headers["location"] == f"/{secret}/read"


def test_settings_offer_the_shortcut_and_it_is_served(client):
    secret = _signup_without_readwise(client)
    page = client.get(f"/{secret}/settings", headers=HTML).text
    assert 'data-copy="#send-address" href="/assets/send-to-matins.shortcut"' in page
    file = client.get("/assets/send-to-matins.shortcut")
    assert file.status_code == 200 and file.content[:4] == b"AEA1"


def test_max_saved_is_a_setting(client):
    secret = _signup_without_readwise(client)
    client.post(
        f"/{secret}/settings",
        data={"csrf": _csrf(client, secret), "schedule": "07:00", "enabled": "1", "max_saved": "25"},
        follow_redirects=False,
    )
    assert client.app.state.matins.get_delivery(secret).max_saved == 25


def test_a_selected_article_opens_as_a_closable_overlay(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article = _reader_article_id(client, secret, "First story")
    page = client.get(f"/{secret}/read", params={"article": article, "view": "all"}, headers=HTML).text
    assert 'class="reader-pane-col open" role="dialog"' in page
    assert f'<a class="btn tiny" href="/{secret}/read' in page and "✕ Close" in page
    assert "reader-pane-col open" not in client.get(f"/{secret}/read", headers=HTML).text


def test_publication_dates_show_only_when_the_source_states_one(client, monkeypatch):
    from datetime import timedelta

    from later_ink.matins.issues import published_label

    assert published_label(None, "Europe/Paris") == ""
    # 23:30 UTC is already the next day in Paris.
    assert published_label(datetime(2026, 11, 1, 23, 30), "Europe/Paris") == "2 Nov 2026"

    _serve_pages(monkeypatch)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="UTC", email=None)
    stamp = (datetime(2026, 3, 4, 12, tzinfo=UTC)).timestamp()
    store.save_url(secret, "https://x.example/dated", "Dated", published=stamp)
    store.save_url(secret, "https://x.example/undated", "Undated")

    rows = client.get(f"/{secret}/read", headers=HTML).text
    assert "4 Mar 2026" in rows
    today = datetime.now(UTC)
    assert f"{today.day} {today:%b %Y}" not in rows
    assert f"{(today - timedelta(days=0)):%d %b}" not in rows
    assert " ago" not in rows

    store.set_show_dates(secret, False)
    assert "4 Mar 2026" not in client.get(f"/{secret}/read", headers=HTML).text


def test_an_issue_prints_the_publication_date_not_a_stand_in():
    from later_ink.matins.issue_epub import _masthead

    when = datetime(2026, 11, 2)
    dated = IssueItem(title="T", html="", section="Le Monde", published="1 Nov 2026", show_dates=True)
    assert "Le Monde — 1 Nov 2026" in _masthead(dated, when)
    bare = IssueItem(title="T", html="", section="Saved", show_dates=True)
    assert "2026" not in _masthead(bare, when)
    off = IssueItem(title="T", html="", section="Le Monde", published="1 Nov 2026")
    assert "Monday 02 November 2026" in _masthead(off, when)


# ------------------------------------------------- freshness and archiving


def test_compose_drops_stale_and_archived_queue_links(client, monkeypatch):
    import time as _time

    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    store.ensure_delivery(secret, schedule="07:00", timezone="Europe/Paris")
    store.set_issue_limits(
        secret, max_articles=None, max_per_feed=None, max_words=None,
        window_hours=24, max_saved=None,
    )
    now = _time.time()
    store.save_url(secret, "https://example.com/fresh", "Fresh", published=now - 3600)
    store.save_url(secret, "https://example.com/old", "Old", published=now - 3 * 86400)
    store.save_url(secret, "https://example.com/gone", "Gone")
    store.archive_article(secret, "https://example.com/gone", title="Gone")

    composed = client.post("/api/v1/issues?only=queue", headers=_api(secret)).json()
    assert {(e["title"], e["reason"]) for e in composed["expired"]} == {
        ("Old", "too old"),
        ("Gone", "archived"),
    }
    assert composed["articles"] == 1
    assert store.list_saved(secret) == []


def test_printed_articles_are_archived_without_a_signal(client):
    secret = _signup_without_readwise(client)
    client.post(f"/{secret}/save", json={"url": "https://example.com/later"})
    client.post("/api/v1/issues?only=queue", headers=_api(secret))

    urls = client.get("/api/v1/archived", headers=_api(secret)).json()["urls"]
    assert urls == ["https://example.com/later"]
    assert client.get("/api/v1/signals", headers=_api(secret)).json()["signals"] == []
    # The edition's own view still shows it as a normal, likeable row.
    issue = client.app.state.matins.list_issues(secret)[0]
    page = client.get(f"/{secret}/read?issue={issue.id}").text
    assert "· archived" not in page


def test_archive_all_keeps_liked_and_queued_and_records_no_signal(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    csrf = _csrf(client, secret)
    first = _reader_article_id(client, secret, "First story")
    client.post(
        f"/{secret}/read/feedback",
        data={"csrf": csrf, "article_id": first, "value": "like", "view": "all"},
    )
    client.post(f"/{secret}/save", json={"url": "https://example.com/mine"})

    done = client.post(
        f"/{secret}/read/archive-all",
        data={"csrf": csrf, "keep": "liked"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    page = client.get(f"/{secret}/read?view=all").text
    assert "First story" in page
    assert "Second story" not in page
    assert client.app.state.matins.list_saved(secret)
    ratings = {
        s["url"]: s["rating"]
        for s in client.get("/api/v1/signals", headers=_api(secret)).json()["signals"]
    }
    assert "not_interested" not in ratings.values()

    client.post(f"/{secret}/read/archive-all", data={"csrf": csrf, "keep": "none"})
    assert "First story" not in client.get(f"/{secret}/read?view=all").text


def test_a_link_only_post_prints_the_linked_article(client, monkeypatch):
    from later_ink.matins.queue import QueueConnector

    fetched = []

    async def fake(url):
        fetched.append(url)
        return _prose()

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    post = (
        '<p>Local privilege escalation in Linux | '
        '<a href="https://news.ycombinator.com/item?id=1">Discussion</a> '
        '<a href="https://openai.com/index/fewer-paths">https://openai.com/index/fewer-paths</a></p>'
    )
    store.save_url(secret, "https://bsky.app/profile/bot/post/1", "Local privilege", post)
    item = store.list_saved(secret)[0]
    reader = QueueConnector(store, secret)
    _article, html = asyncio.run(reader.get_article_html(str(item.id)))
    assert fetched == ["https://openai.com/index/fewer-paths"]
    assert "sentence, with enough text" in html


def test_a_post_with_its_own_text_is_printed_as_is(client, monkeypatch):
    from later_ink.matins.queue import QueueConnector

    async def fake(url):
        raise AssertionError("nothing to fetch")

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    post = "<p>" + "commentary " * 40 + '<a href="https://example.com/a">link</a></p>'
    store.save_url(secret, "https://bsky.app/profile/x/post/2", "Thoughts", post)
    item = store.list_saved(secret)[0]
    _article, html = asyncio.run(QueueConnector(store, secret).get_article_html(str(item.id)))
    assert html == post


def test_the_reader_shows_the_article_a_link_only_post_points_at(client, monkeypatch):
    from later_ink.matins import queue

    queue._linked.clear()
    fetched = []

    async def fake(url):
        fetched.append(url)
        return _prose()

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    secret = _signup_without_readwise(client)
    title = "Local privilege escalation in Linux | Discussion"
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "hn1",
                    "url": "https://bsky.app/profile/bot/post/1",
                    "title": title,
                    "source": "@hackernewsbot",
                    "content": (
                        '<p>Local privilege escalation in Linux | '
                        '<a href="https://news.ycombinator.com/item?id=1">Discussion</a> '
                        '<a href="https://example.org/lpe">https://example.org/lpe</a></p>'
                    ),
                    "published": datetime.now(UTC).timestamp(),
                }
            ]
        },
    )
    page = client.get(f"/{secret}/read?article=cand%3Ahn1").text
    assert "sentence, with enough text" in page
    assert "https://example.org/lpe</a>" not in page
    client.get(f"/{secret}/read?article=cand%3Ahn1")
    assert fetched == ["https://example.org/lpe"]  # cached the second time


def test_a_publisher_that_refuses_servers_is_read_from_the_wayback_machine(monkeypatch):
    from later_ink.matins import queue

    queue._linked.clear()
    fetched = []

    async def fake(url):
        fetched.append(url)
        return _prose() if url.startswith("https://web.archive.org/") else None

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    post = '<p>Title | <a href="https://openai.com/index/x/">https://openai.com/index/x/</a></p>'
    content = asyncio.run(queue.linked_content(post))
    assert "sentence, with enough text" in content
    assert fetched == [
        "https://openai.com/index/x/",
        "https://web.archive.org/web/2id_/https://openai.com/index/x/",
    ]


def test_a_first_line_that_repeats_the_title_is_dropped():
    from later_ink.matins.routes import _drop_repeated_title

    body = "<h1>Local privilege escalation</h1><p>The article.</p>"
    assert _drop_repeated_title(body, "Local privilege escalation | Discussion") == "<p>The article.</p>"
    alone = "<p>Local privilege escalation</p><figure><img src='https://x/y.png'></figure>"
    assert _drop_repeated_title(alone, "Local privilege escalation") == alone
    other = "<p>Something else entirely.</p><p>More.</p>"
    assert _drop_repeated_title(other, "Local privilege escalation") == other


def test_read_more_lines_point_at_the_article():
    from later_ink.matins.extract import read_more_link

    assert read_more_link(
        '<p>Teaser.</p><p>Read the full article on <a href="https://lemonde.fr/a">Le Monde</a></p>'
    ) == "https://lemonde.fr/a"
    assert read_more_link('<p>Résumé. <a href="https://x.fr/b">Lire la suite</a></p>') == "https://x.fr/b"
    assert read_more_link('<p><a href="https://x.it/c">Leggi tutto</a></p>') == "https://x.it/c"
    # "Read also" points at another article; long prose is not a pointer.
    assert read_more_link('<p><a href="https://x.it/d">Leggi anche</a>: altro</p>') is None
    prose = "<p>" + "word " * 30 + 'and you can read more <a href="https://x.com/e">here</a></p>'
    assert read_more_link(prose) is None


def test_a_teaser_is_replaced_only_by_a_longer_article(monkeypatch):
    from later_ink.matins import queue

    queue._linked.clear()
    pages = {"https://news.example/long": _prose(), "https://news.example/short": "<p>Cookies?</p>"}

    async def fake(url):
        return pages.get(url)

    monkeypatch.setattr("later_ink.matins.queue._fetch_page", fake)
    teaser = "<p>" + "teaser " * 40 + '</p><p>Continue reading on <a href="{}">News</a></p>'
    long = asyncio.run(queue.linked_content(teaser.format("https://news.example/long")))
    assert "sentence, with enough text" in long
    assert asyncio.run(queue.linked_content(teaser.format("https://news.example/short"))) is None


def test_a_source_can_be_switched_off_and_back_on(client):
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={"items": [{"id": "sw1", "url": "https://bsky.app/profile/a/post/5", "title": "A Bluesky post",
                         "source": "@a", "content": "<p>Body</p>", "published": datetime.now(UTC).timestamp()}]},
    )
    assert "A Bluesky post" in client.get(f"/{secret}/read").text
    csrf = _csrf(client, secret)
    client.post(f"/{secret}/read/sources", data={"csrf": csrf, "toggle": "Bluesky"}, follow_redirects=False)
    page = client.get(f"/{secret}/read").text
    assert "A Bluesky post" not in page
    assert "First story" in page  # the feed is still on
    assert 'aria-pressed="false"' in page  # and Bluesky is still listed, to switch back on
    # Archive all leaves the hidden source alone.
    client.post(f"/{secret}/read/archive-all", data={"csrf": csrf, "keep": "none"})
    client.post(f"/{secret}/read/sources", data={"csrf": csrf, "toggle": ""}, follow_redirects=False)
    assert "A Bluesky post" in client.get(f"/{secret}/read").text


def test_safari_gets_require_corp_and_relayed_images(client, monkeypatch):
    from later_ink.matins import routes

    secret = _signup_without_readwise(client)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={"items": [{"id": "im1", "url": "https://bsky.app/profile/a/post/7", "title": "Picture post",
                         "source": "@a", "content": "<p>Look at this long enough caption here.</p>"
                         "<figure><img src='https://cdn.example/pic.jpg' srcset='https://cdn.example/pic2.jpg 2x'></figure>",
                         "published": datetime.now(UTC).timestamp()}]},
    )
    safari = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1"}
    page = client.get(f"/{secret}/read", params={"article": "cand:im1"}, headers=safari)
    assert page.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    match = re.search(r'src="(/assets/img\?u=[^"]+)"', page.text)
    assert match and "srcset" not in page.text.split('class="article"')[1]
    relay = match.group(1).replace("&amp;", "&")

    async def fake(client_, url, **kwargs):
        assert url == "https://cdn.example/pic.jpg"
        return b"\xff\xd8jpeg", "image/jpeg"

    monkeypatch.setattr(routes, "fetch_bytes", fake)
    got = client.get(relay)
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    # Only what the server signed is fetched.
    assert client.get(relay.replace("pic.jpg", "other.jpg")).status_code == 404
    chrome = client.get(f"/{secret}/read", params={"article": "cand:im1"})
    assert chrome.headers["Cross-Origin-Embedder-Policy"] == "credentialless"
    assert "https://cdn.example/pic.jpg" in chrome.text


def _fake_taste(monkeypatch, topics=("eink", "azure", "football", "battery")):
    """Embeddings from topic words, so similarity is predictable."""
    import numpy as np

    from later_ink.matins import embed, taste

    def vector(text):
        words = text.casefold().replace("-", "").split()
        v = np.array([sum(w.startswith(t) for w in words) for t in topics], dtype=np.float32) + 0.01
        return v / np.linalg.norm(v)

    monkeypatch.setattr(embed, "embedder", lambda: object())
    monkeypatch.setattr(embed, "vector", vector)
    taste._cache.clear()


def test_taste_ranks_what_is_closest_to_what_was_liked(client, monkeypatch):
    from later_ink.matins import taste

    _fake_taste(monkeypatch)
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    assert taste.taste_for(store, secret) is None  # nothing to learn from yet
    for i, title in enumerate(["E-ink tablet review", "Kindle e-ink news", "Azure outage report"]):
        store.record_event(secret, f"https://x.test/{i}", article_id=None, title=title, source="s", kind="opened")
    store.record_event(secret, "https://x.test/f", article_id=None, title="Football results", source="s", kind="read")
    store.set_signal(secret, "https://x.test/f", -1, title="Football results", source="s")
    profile = taste.taste_for(store, secret)
    assert profile is not None
    assert profile.score("New e-ink phone") > profile.score("Battery chemistry")
    assert profile.score("Battery chemistry") > profile.score("Football transfer news")

    scored = client.post(
        "/api/v1/score",
        headers=_api(secret),
        json={"items": [{"id": "a", "title": "e-ink reader"}, {"id": "b", "title": "football derby"}]},
    ).json()
    assert scored["scores"]["a"] > scored["scores"]["b"]


def test_an_automatic_issue_prefers_what_the_reader_likes(client, monkeypatch):
    _fake_taste(monkeypatch, topics=("first", "second"))
    secret = _signup_without_readwise(client)
    store = client.app.state.matins
    _add_feed(client, secret)
    store.set_issue_limits(secret, max_articles=None, max_per_feed=1, max_words=None, window_hours=None)
    preview = client.get(f"/{secret}/issues/preview").text
    assert "First story" in preview and "Second story" not in preview  # feed order
    for i in range(3):
        store.record_event(secret, f"https://x.test/{i}", title=f"Second thoughts {i}", source="s", kind="opened")
    preview = client.get(f"/{secret}/issues/preview").text
    assert "Second story" in preview and "First story" not in preview


def test_score_is_empty_without_a_taste(client):
    secret = _signup_without_readwise(client)
    scored = client.post("/api/v1/score", headers=_api(secret), json={"items": [{"id": "a", "title": "x"}]})
    assert scored.json() == {"scores": {}, "signals": 0}
    assert client.post("/api/v1/score", headers=_api(secret), json={"items": "nope"}).status_code == 400


def test_the_timezone_is_picked_from_a_list(client):
    secret = _signup_without_readwise(client)
    page = client.get(f"/{secret}/settings", headers=HTML).text
    assert '<select id="timezone" name="timezone">' in page
    assert '<optgroup label="Europe">' in page
    assert re.search(r'<option value="Europe/Paris"( selected)?>Paris \(UTC\+0[12]:00\)</option>', page)
    assert "US/Eastern" not in page


# ------------------------------------------------------------------- listening


def _tts_dir(tmp_path, monkeypatch):
    root = tmp_path / "tts"
    (root / "model" / "voices").mkdir(parents=True)
    (root / "kokoro.web.js").write_text("export {};")
    (root / "model" / "voices" / "ff_siwis.bin").write_bytes(b"\0" * 8)
    monkeypatch.setenv("TTS_ASSETS_DIR", str(root))
    return root


def test_listen_to_all_reads_each_listed_article_title_first(client, tmp_path, monkeypatch):
    _tts_dir(tmp_path, monkeypatch)
    secret = _signup_without_readwise(client)
    client.post(
        "/api/v1/candidates",
        headers=_api(secret),
        json={
            "items": [
                {
                    "id": "p1",
                    "url": "https://bsky.app/profile/a/post/1",
                    "title": "A post",
                    "source": "@a",
                    "content": "<p>First paragraph.</p><ul><li>One point</li></ul><pre><p>code</p></pre>",
                    "published": datetime.now(UTC).timestamp(),
                }
            ]
        },
    )
    page = client.get(f"/{secret}/read").text
    assert "▶ Listen to all" in page
    assert f'data-source="/{secret}/read/listen"' in page
    spoken = client.get(f"/{secret}/read/listen", params={"article": "cand:p1"}).json()
    assert spoken["blocks"] == ["A post", "First paragraph.", "One point"]
    assert spoken["voice"] == "af_heart"
    assert client.get(f"/{secret}/read/listen", params={"article": "cand:nope"}).status_code == 404


def test_listen_to_all_is_off_without_the_voice_files(client, monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_ASSETS_DIR", str(tmp_path / "none"))
    secret = _signup_without_readwise(client)
    assert client.get(f"/{secret}/read/listen", params={"article": "x"}).status_code == 404


def test_listening_reads_each_language_with_its_voice(client, tmp_path, monkeypatch):
    _tts_dir(tmp_path, monkeypatch)
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    store = client.app.state.matins
    article = _reader_article_id(client, secret, "First story")

    def bar():
        page = client.get(f"/{secret}/read", params={"article": article}, headers=HTML).text
        match = re.search(r'class="listen" hidden data-voice="([a-z_]+)"', page)
        return match and match.group(1)

    assert bar() == "af_heart"
    settings = client.get(f"/{secret}/settings", headers=HTML).text
    assert 'name="voice_en"' in settings and 'name="voice_it"' not in settings
    # Add Italian, keeping the English voice picked meanwhile.
    client.post(
        f"/{secret}/settings/languages",
        data={"csrf": _csrf(client, secret), "voice_en": "bm_george", "add_language": "it", "add": "1"},
        follow_redirects=False,
    )
    assert 'name="voice_it"' in client.get(f"/{secret}/settings", headers=HTML).text
    client.post(
        f"/{secret}/settings",
        data={"csrf": _csrf(client, secret), "schedule": "07:00", "voice_en": "bm_george", "voice_it": "im_nicola"},
        follow_redirects=False,
    )
    assert bar() == "bm_george"

    # The feed is Italian: its articles take the Italian voice.
    feed = store.list_feeds(secret)[0]
    saved = client.post(
        f"/{secret}/feeds/{feed.id}/voice",
        data={"csrf": _csrf(client, secret), "voice": "it"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert bar() == "im_nicola"
    client.post(
        f"/{secret}/feeds/{feed.id}/voice",
        data={"csrf": _csrf(client, secret), "voice": ""},
        follow_redirects=False,
    )
    assert store.get_feed(secret, feed.id).voice is None
    assert bar() == "bm_george"

    client.post(
        f"/{secret}/settings/languages",
        data={"csrf": _csrf(client, secret), "voice_en": "bm_george", "voice_it": "im_nicola", "remove_language": "it"},
        follow_redirects=False,
    )
    assert 'name="voice_it"' not in client.get(f"/{secret}/settings", headers=HTML).text


def test_the_language_is_detected_from_the_text():
    from later_ink.matins.voices import detect_language, parse_choices

    assert detect_language("Il governo ha deciso che la legge della regione non è valida.") == "it"
    assert detect_language("Le gouvernement a annoncé une conférence avec les syndicats.") == "fr"
    assert detect_language("Hello") is None
    # A catalogue that saved one voice before languages existed.
    assert parse_choices("ff_siwis") == {"fr": "ff_siwis"}


def test_an_unknown_voice_is_refused(client, tmp_path, monkeypatch):
    _tts_dir(tmp_path, monkeypatch)
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    store = client.app.state.matins
    for bad_voice in ("../etc", "ff_siwis"):  # unknown, or not English
        bad = client.post(
            f"/{secret}/settings",
            data={"csrf": _csrf(client, secret), "schedule": "07:00", "voice_en": bad_voice},
            follow_redirects=False,
        )
        assert bad.status_code == 400
    feed = store.list_feeds(secret)[0]
    client.post(
        f"/{secret}/feeds/{feed.id}/voice",
        data={"csrf": _csrf(client, secret), "voice": "zz"},
        follow_redirects=False,
    )
    assert store.get_feed(secret, feed.id).voice is None


def test_without_the_tts_files_there_is_no_listen_button(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_ASSETS_DIR", str(tmp_path / "missing"))
    secret = _signup_without_readwise(client)
    _add_feed(client, secret)
    article = _reader_article_id(client, secret, "First story")
    page = client.get(f"/{secret}/read", params={"article": article}, headers=HTML).text
    assert 'class="listen"' not in page
    assert 'name="voice_en"' not in client.get(f"/{secret}/settings", headers=HTML).text
    assert client.get("/assets/tts/worker.js").status_code == 404


def test_the_reader_policy_allows_the_player_and_its_worker(client):
    from later_ink.matins.routes import READER_CSP

    assert "worker-src 'self'" in READER_CSP
    assert READER_CSP.count("'sha256-") == 3


def test_tts_files_are_served_with_a_worker_policy_and_nothing_else(client, tmp_path, monkeypatch):
    _tts_dir(tmp_path, monkeypatch)
    (tmp_path / "secret.txt").write_text("no")
    worker = client.get("/assets/tts/worker.js")
    assert worker.status_code == 200
    assert worker.headers["content-type"].startswith("text/javascript")
    assert "'wasm-unsafe-eval'" in worker.headers["content-security-policy"]
    assert "connect-src 'self'" in worker.headers["content-security-policy"]
    voice = client.get("/assets/tts/model/voices/ff_siwis.bin")
    assert voice.status_code == 200 and voice.content == b"\0" * 8
    assert client.get("/assets/tts/../secret.txt").status_code == 404
    assert client.get("/assets/tts/%2e%2e/secret.txt").status_code == 404
    assert client.get("/assets/tts/model/voices/nope.bin").status_code == 404


def _hn_xml(*stories):
    items = "".join(
        f"<item><title>{title}</title><link>https://example.com/{title}</link>"
        f"<pubDate>{posted}</pubDate></item>"
        for title, posted in stories
    )
    return f"<rss version='2.0'><channel><title>Hacker News</title>{items}</channel></rss>".encode()


def test_a_story_reaching_the_front_page_is_dated_when_it_arrived(tmp_path, monkeypatch):
    from later_ink.matins import rss as rss_module

    store = MatinsStore(f"{tmp_path}/app.db", Fernet(Fernet.generate_key()))
    feed = store.add_feed("cat", "https://news.ycombinator.com/rss")
    old = "Thu, 05 Nov 2026 08:00:00 +0000"
    served = {"xml": _hn_xml(("first", old))}

    async def fake_fetch(url, *, timeout, max_bytes):
        return served["xml"]

    monkeypatch.setattr(rss_module, "fetch_feed", fake_fetch)

    def items():
        clear_cache()
        parsed = asyncio.run(rss_module.RssConnector(store, "cat")._load(feed))
        return {item.title: item.published for item in parsed.items}

    # What the feed carried when Matins first read it keeps its own date.
    assert items()["first"] == datetime(2026, 11, 5, 8, 0)
    # A story posted yesterday that reaches the front page now is dated now.
    served["xml"] = _hn_xml(("first", old), ("risen", "Thu, 05 Nov 2026 09:00:00 +0000"))
    dated = items()
    assert dated["first"] == datetime(2026, 11, 5, 8, 0)
    assert dated["risen"] > datetime(2026, 11, 6)
    # A story fetched minutes after it was posted keeps its own date.
    just = datetime.now(UTC).replace(microsecond=0, tzinfo=None) - timedelta(minutes=5)
    served["xml"] = _hn_xml(("fresh", just.strftime("%a, %d %b %Y %H:%M:%S +0000")))
    assert items()["fresh"] == just


