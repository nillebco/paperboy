"""Server-rendered pages, sharing one small design system.

Deliberately no JavaScript and no external assets: the app's CSP forbids
remote anything, and these pages are read on the same e-ink devices the
catalogue serves. What they do have is a hierarchy — a dashboard that answers
"what is in here and what happens next", a delivery form, and a preview that
shows the picks before anything is written.
"""

from html import escape
from urllib.parse import urlsplit

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf7; --fg: #1c1b18; --muted: #6b6862; --line: #dcd8d0;
  --card: #ffffff; --accent: #1c1b18; --accent-fg: #fbfaf7; --warn: #8a2b1f;
  --ok: #1a6b32;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14140f; --fg: #e8e4da; --muted: #9a958a; --line: #33322b;
    --card: #1c1b16; --accent: #e8e4da; --accent-fg: #14140f; --warn: #e08b7d;
    --ok: #6fcf8a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 16px/1.55 ui-serif, Georgia, "Iowan Old Style", serif;
  max-width: 44rem; margin-inline: auto;
}
h1 { font-size: 1.6rem; margin: 0 0 .15em; letter-spacing: -.01em; }
h2 { font-size: .78rem; margin: 2.4rem 0 .7rem; text-transform: uppercase;
     letter-spacing: .1em; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }
h3 { font-size: 1rem; margin: 0 0 .2em; }
p { margin: .5em 0; }
a { color: inherit; text-decoration-thickness: .06em; text-underline-offset: .18em; }
a:hover { opacity: .75; }
.small { font-size: .9rem; }
.meta { font-size: .82rem; color: var(--muted); }
.muted { color: var(--muted); }
.err { color: var(--warn); font-size: .9rem; }
.ok { color: var(--ok); font-size: .9rem; }
code, .url {
  font: .82rem/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: color-mix(in srgb, var(--fg) 7%, transparent);
  padding: .18em .4em; border-radius: 4px; overflow-wrap: anywhere;
}
.url-block { display: block; margin: .5rem 0 0; padding: .6rem .75rem; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.1rem; margin: .6rem 0;
}
.row {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: .75rem; padding: .55rem 0; border-top: 1px solid var(--line);
}
.row:first-of-type { border-top: 0; }
.row-main { min-width: 0; }
.row-title { display: block; font-size: .95rem; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }
.row-sub { font-size: .8rem; color: var(--muted); overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: .5rem; margin: .9rem 0 0; }
.btn {
  font: .9rem/1 ui-sans-serif, system-ui, sans-serif; padding: .6em 1em;
  border: 1px solid var(--line); border-radius: 7px; background: transparent;
  color: inherit; cursor: pointer; text-decoration: none; display: inline-block;
}
.btn:hover { opacity: .75; }
.btn.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.btn.tiny { font-size: .78rem; padding: .35em .65em; }
.btn.danger { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, transparent); }
label { display: block; margin: 1.1rem 0 .3rem; font-size: .82rem; font-weight: 600;
        font-family: ui-sans-serif, system-ui, sans-serif; }
input[type=text], input[type=email], input[type=url], input[type=time], textarea {
  width: 100%; padding: .55rem .6rem; font: inherit; color: inherit;
  background: var(--bg); border: 1px solid var(--line); border-radius: 7px;
}
input:focus-visible, button:focus-visible, a:focus-visible {
  outline: 2px solid var(--fg); outline-offset: 2px;
}
.inline-form { display: flex; gap: .5rem; margin-top: .6rem; }
.inline-form input { flex: 1; }
.inline-form .btn { white-space: nowrap; }
.count { font-variant-numeric: tabular-nums; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2rem 0; }
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _minutes(words: int) -> str:
    minutes = max(1, round(words / 250))
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def dashboard_page(
    *,
    secret: str,
    base_url: str,
    csrf: str,
    feeds: list[dict],
    saved: list[dict],
    issues: list[dict],
    schedule: str,
    timezone: str,
    scheduling: bool,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """The catalogue's home page.

    Answers three questions in order: what will come next, what is it made from,
    and what has already been printed. Sources live here rather than in
    settings, because adding a feed is the thing you do most.
    """
    banner = ""
    if notice:
        banner = f'<p class="ok">{escape(notice)}</p>'
    if error:
        banner = f'<p class="err">{escape(error)}</p>'

    if scheduling:
        schedule_line = (
            f'Every day at <strong>{escape(schedule)}</strong> · {escape(timezone)}'
        )
    else:
        schedule_line = "Scheduling is <strong>off</strong> — issues are built when you ask."

    feed_rows = "".join(
        f"""
<div class="row">
  <div class="row-main">
    <span class="row-title">{escape(feed["title"])}</span>
    <span class="row-sub">{escape(_host(feed["url"]))}</span>
  </div>
  <form method="post" action="/{escape(secret)}/feeds/{escape(feed["id"])}/delete">
    <input type="hidden" name="csrf" value="{escape(csrf)}">
    <button class="btn tiny" type="submit">Remove</button>
  </form>
</div>"""
        for feed in feeds
    ) or '<p class="meta">No feeds yet.</p>'

    saved_rows = "".join(
        f"""
<div class="row">
  <div class="row-main">
    <span class="row-title">{escape(item["title"])}</span>
    <span class="row-sub">{escape(item["url"])}</span>
  </div>
  <form method="post" action="/{escape(secret)}/saved/{escape(str(item["id"]))}/delete">
    <input type="hidden" name="csrf" value="{escape(csrf)}">
    <button class="btn tiny" type="submit">Remove</button>
  </form>
</div>"""
        for item in saved[:10]
    ) or '<p class="meta">Nothing saved. Paste a link below and it goes into the next issue.</p>'

    issue_rows = "".join(
        f"""
<div class="row">
  <div class="row-main">
    <a class="row-title" href="/{escape(secret)}/issues/{escape(issue["id"])}.epub">{escape(issue["title"])}</a>
    <span class="row-sub">{_plural(issue["count"], "article")} · about {_minutes(issue["words"])}</span>
  </div>
</div>"""
        for issue in issues
    ) or '<p class="meta">No issues yet. Compose one once you have feeds.</p>'

    body = f"""
<h1>Matins</h1>
<p class="meta">Catalogue <code>/{escape(secret)}/</code> · {_plural(len(feeds), "feed")} ·
{_plural(len(saved), "saved link")} · {_plural(len(issues), "issue")}</p>
{banner}

<div class="card">
  <h2 style="margin-top:0">Next issue</h2>
  <p>{schedule_line}</p>
  <div class="actions">
    <a class="btn primary" href="/{escape(secret)}/issues/preview">Preview next issue</a>
    <form method="post" action="/{escape(secret)}/issues/build">
      <input type="hidden" name="csrf" value="{escape(csrf)}">
      <button class="btn" type="submit">Compose now</button>
    </form>
    <a class="btn" href="/{escape(secret)}/settings">Delivery settings</a>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0">Reader</h2>
  <p class="small">Read your feeds and saved links here, without building an issue.</p>
  <div class="actions"><a class="btn" href="/{escape(secret)}/read">Open the reader</a></div>
</div>

<h2>Feeds</h2>
<div class="card">{feed_rows}
  <form class="inline-form" method="post" action="/{escape(secret)}/feeds">
    <input type="hidden" name="csrf" value="{escape(csrf)}">
    <input type="url" name="url" placeholder="https://example.com/feed.xml" required>
    <button class="btn" type="submit">Add feed</button>
  </form>
</div>

<h2>Saved for later</h2>
<div class="card">{saved_rows}
  <form class="inline-form" method="post" action="/{escape(secret)}/save">
    <input type="url" name="url" placeholder="https://example.com/article" required>
    <button class="btn" type="submit">Save</button>
  </form>
</div>

<h2>Issues</h2>
<div class="card">{issue_rows}</div>

<h2>Your reader</h2>
<div class="card">
  <p class="small">Add this to KOReader, CrossPoint or any OPDS reader. It serves
  the catalogue; this page is for you.</p>
  <span class="url url-block">{escape(base_url)}/{escape(secret)}/</span>
</div>

<hr>
<form method="post" action="/{escape(secret)}/forget">
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <button class="btn tiny" type="submit">Forget this catalogue on this device</button>
</form>
<p class="meta">Removes it from this browser's list only — the catalogue keeps
working everywhere else.</p>
"""
    return _page("Matins", body)


def settings_page(
    *,
    secret: str,
    csrf: str,
    email: str,
    webhook_url: str,
    schedule: str,
    timezone: str,
    enabled: bool,
    webhook_secret_set: bool,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    banner = ""
    if notice:
        banner = f'<p class="ok">{escape(notice)}</p>'
    if error:
        banner = f'<p class="err">{escape(error)}</p>'

    secret_note = (
        "A signing secret is set — leave blank to keep it."
        if webhook_secret_set
        else "Optional. The receiver uses it to verify the signature."
    )

    body = f"""
<p class="meta"><a href="/{escape(secret)}/">&larr; Back to the catalogue</a></p>
<h1>Delivery</h1>
<p class="small">When the issue is ready, Matins can call a webhook and send you an
email. Neither carries the issue itself — that is pulled by your reader.</p>
{banner}

<form method="post" action="/{escape(secret)}/settings">
  <input type="hidden" name="csrf" value="{escape(csrf)}">

  <div class="card">
    <h2 style="margin-top:0">Schedule</h2>
    <label for="schedule">Compose an issue at</label>
    <input type="time" id="schedule" name="schedule" value="{escape(schedule)}">
    <label for="timezone">Timezone</label>
    <input type="text" id="timezone" name="timezone" value="{escape(timezone)}"
      placeholder="Europe/Paris">
    <label style="margin-top:1.2rem">
      <input type="checkbox" name="enabled" value="1" {'checked' if enabled else ''}>
      Compose and deliver automatically
    </label>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Email</h2>
    <label for="email">Send a notification to</label>
    <input type="email" id="email" name="email" value="{escape(email)}"
      placeholder="you@example.com">
    <p class="meta">Just a subject line and a link. The issue is never attached.</p>
  </div>

  <div class="card">
    <h2 style="margin-top:0">Webhook</h2>
    <label for="webhook_url">POST to</label>
    <input type="url" id="webhook_url" name="webhook_url" value="{escape(webhook_url)}"
      placeholder="https://example.com/hooks/matins">
    <label for="webhook_secret">Signing secret</label>
    <input type="text" id="webhook_secret" name="webhook_secret" value=""
      placeholder="{escape('unchanged' if webhook_secret_set else 'whsec-like value')}">
    <p class="meta">{escape(secret_note)} Signed
    <code>t=&lt;unix&gt;,v1=&lt;hmac-sha256&gt;</code> over
    <code>timestamp.body</code> in <code>X-Matins-Signature</code>.</p>
  </div>

  <div class="actions"><button class="btn primary" type="submit">Save</button></div>
</form>
"""
    return _page("Delivery settings", body)


def preview_page(
    *,
    secret: str,
    csrf: str,
    candidates: list[dict],
    window_hours: int,
    max_articles: int,
) -> str:
    """What the next issue would contain, before anything is written."""
    header = f"""
<p class="meta"><a href="/{escape(secret)}/">&larr; Back to the catalogue</a></p>
<h1>Next issue</h1>"""

    if not candidates:
        return _page(
            "Next issue",
            f"""{header}
<div class="card">
  <p>Nothing is queued.</p>
  <p class="meta">An issue is built from feed items published in the last
  {window_hours} hours that have not already been printed, plus anything saved
  for later. Add a feed or save a link on the catalogue page.</p>
  <div class="actions"><a class="btn" href="/{escape(secret)}/">Add feeds</a></div>
</div>""",
        )

    total_words = sum(c["words"] for c in candidates)
    sections: dict[str, list[dict]] = {}
    for candidate in candidates:
        sections.setdefault(candidate["section"], []).append(candidate)

    blocks = ""
    for section, items in sections.items():
        rows = "".join(
            f"""
<div class="row">
  <div class="row-main">
    <span class="row-title">{escape(item["title"])}</span>
    <span class="row-sub">about {_minutes(item["words"])}
    {' · saved for later' if item["source"] == "later" else ''}</span>
  </div>
</div>"""
            for item in items
        )
        blocks += f'<h2>{escape(section)}</h2><div class="card">{rows}</div>'

    body = f"""{header}
<p class="small"><span class="count">{_plural(len(candidates), "article")}</span> ·
about {_minutes(total_words)} of reading.</p>
<p class="meta">Newest first, at most {max_articles} articles, with a per-feed
cap and a word budget.</p>

{blocks}

<div class="actions">
  <form method="post" action="/{escape(secret)}/issues/build">
    <input type="hidden" name="csrf" value="{escape(csrf)}">
    <button class="btn primary" type="submit">Compose this issue</button>
  </form>
  <a class="btn" href="/{escape(secret)}/">Change sources</a>
</div>
<p class="meta">Word counts come from the feed; extraction may differ slightly.</p>
"""
    return _page("Next issue", body)


def notice_page(title: str, message: str, back_href: str) -> str:
    return _page(
        title,
        f"""<h1>{escape(title)}</h1>
<div class="card"><p>{escape(message)}</p>
<p><a href="{escape(back_href)}">Back to the catalogue</a></p></div>""",
    )


_READER_CSS = """
.reader-head { display: flex; align-items: baseline; justify-content: space-between;
  gap: 1rem; flex-wrap: wrap; }
.tabs { display: flex; gap: 1rem; margin: 1rem 0 0; font-size: .9rem; }
.tabs a[aria-current] { font-weight: 700; text-decoration: none; }
.article { font-size: 1.05rem; }
.article h1, .article h2, .article h3 { font-family: inherit; text-transform: none;
  letter-spacing: 0; color: inherit; margin: 1.6em 0 .4em; font-size: 1.2rem; }
.article p { margin: .9em 0; }
.article img { max-width: 100%; height: auto; border-radius: 6px; }
.article blockquote { margin: 1.2em 0; padding-left: 1em; border-left: 3px solid var(--line);
  color: var(--muted); }
.article pre { overflow-x: auto; font-size: .85rem; }
.article a { text-decoration: underline; }
.reader-meta { font-size: .85rem; color: var(--muted); margin-bottom: 1.4rem; }
"""


def reader_page(
    *,
    secret: str,
    csrf: str,
    items: list[dict],
    total: int,
    unread: int,
    show_all: bool,
) -> str:
    """Unread list across every source, newest first."""
    rows = "".join(
        f"""
<div class="row">
  <div class="row-main">
    <a class="row-title" style="text-decoration:none"
       href="/{escape(secret)}/read/{escape(item["id"])}">{escape(item["title"])}</a>
    <span class="row-sub">{escape(item["source"])} · about {_minutes(item["minutes"] * 250)}
    {' · saved' if item["saved"] else ''}</span>
  </div>
</div>"""
        for item in items
    ) or (
        '<p class="meta">Nothing unread. '
        f'<a href="/{escape(secret)}/read?all=1">Show everything</a>.</p>'
        if not show_all
        else '<p class="meta">No items yet — add a feed or save a link.</p>'
    )

    body = f"""
<p class="meta"><a href="/{escape(secret)}/">&larr; Back to the catalogue</a></p>
<div class="reader-head">
  <h1 style="margin:0">Reader</h1>
  <p class="meta count" style="margin:0">{unread} unread of {total}</p>
</div>
<div class="tabs">
  <a href="/{escape(secret)}/read" {'aria-current="page"' if not show_all else ''}>Unread</a>
  <a href="/{escape(secret)}/read?all=1" {'aria-current="page"' if show_all else ''}>All</a>
</div>
<style>{_READER_CSS}</style>
<div class="card" style="margin-top:1rem">{rows}</div>
<form class="actions" method="post" action="/{escape(secret)}/read/mark-all">
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <button class="btn tiny" type="submit">Mark everything as read</button>
</form>
"""
    return _page("Reader", body)


def reader_article_page(
    *,
    secret: str,
    csrf: str,
    article_id: str,
    title: str,
    author: str | None,
    source_url: str | None,
    body: str,
    minutes: int,
) -> str:
    """One article, rendered inside the app instead of on the publisher's page."""
    meta = [f"about {minutes} minute" + ("" if minutes == 1 else "s")]
    if author:
        meta.insert(0, escape(author))
    original = (
        f'<a class="btn tiny" href="{escape(source_url)}" target="_blank" '
        'rel="noopener noreferrer">Open original</a>'
        if source_url
        else ""
    )
    save_form = (
        f"""
<form method="post" action="/{escape(secret)}/save">
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <input type="hidden" name="url" value="{escape(source_url)}">
  <button class="btn tiny" type="submit">Save for later</button>
</form>"""
        if source_url
        else ""
    )
    return _page(
        title,
        f"""
<style>{_READER_CSS}</style>
<p class="meta"><a href="/{escape(secret)}/read">&larr; Back to the reader</a></p>
<h1>{escape(title)}</h1>
<p class="reader-meta">{' · '.join(meta)}</p>
<div class="article">{body}</div>
<div class="actions">
  {original}
  {save_form}
  <form method="post" action="/{escape(secret)}/read/{escape(article_id)}/unread">
    <input type="hidden" name="csrf" value="{escape(csrf)}">
    <button class="btn tiny" type="submit">Mark unread</button>
  </form>
  <a class="btn tiny" href="/{escape(secret)}/read">Next unread</a>
</div>
""",
    )


def signin_page(*, notice: str | None = None, error: str | None = None) -> str:
    banner = ""
    if notice:
        banner = f'<p class="ok">{escape(notice)}</p>'
    if error:
        banner = f'<p class="err">{escape(error)}</p>'
    body = f"""
<h1>Sign in</h1>
<p class="small">Enter your email and we will send you a six-digit code. The same
address always opens the same catalogue — on any device, no link to keep.</p>
{banner}
<form method="post" action="/signin">
  <label for="email">Email</label>
  <input type="email" id="email" name="email" placeholder="you@example.com" required>
  <div class="actions"><button class="btn primary" type="submit">Send me a code</button></div>
</form>
<p class="meta">First time? The same form creates a catalogue for that address.</p>
<p class="meta"><a href="/start">Prefer a Readwise token, or no email at all &rarr;</a></p>
"""
    return _page("Sign in", body)


def code_page(*, email: str, error: str | None = None) -> str:
    banner = f'<p class="err">{escape(error)}</p>' if error else ""
    body = f"""
<h1>Check your email</h1>
<p class="small">We sent a six-digit code to <strong>{escape(email)}</strong>.
It expires in 10 minutes.</p>
{banner}
<form method="post" action="/signin/code">
  <input type="hidden" name="email" value="{escape(email)}">
  <label for="code">Code</label>
  <input type="text" id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
    pattern="[0-9]*" maxlength="6" placeholder="123456" required autofocus>
  <div class="actions"><button class="btn primary" type="submit">Sign in</button></div>
</form>
<form method="post" action="/signin">
  <input type="hidden" name="email" value="{escape(email)}">
  <div class="actions"><button class="btn tiny" type="submit">Send another code</button></div>
</form>
<p class="meta"><a href="/signin">Use a different address</a></p>
"""
    return _page("Check your email", body)
