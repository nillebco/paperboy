"""Server-rendered pages, sharing one shell and one small design system.

Deliberately no JavaScript and no external assets: the app's CSP forbids remote
anything, and these pages are read on the same e-ink devices the catalogue
serves. Confirmations are therefore <details> disclosures rather than dialogs.

Every catalogue page goes through `_shell`, which is what makes them one app: the
same masthead, the same navigation, the same place for a message after an action.
"""

from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from time import time
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, available_timezones

from ..pages import _COPY_JS as COPY_JS

REPO_URL = "https://github.com/nillebco/paperboy"

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf7; --fg: #1c1b18; --muted: #63605a; --line: #d8d4cb; --soft: #f1eee7;
  --card: #ffffff; --accent: #1c1b18; --accent-fg: #fbfaf7; --warn: #8a2b1f;
  --ok: #1a6b32;
  --serif: ui-serif, Georgia, "Iowan Old Style", serif;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14140f; --fg: #e8e4da; --muted: #a29d91; --line: #38372f; --soft: #1f1e18;
    --card: #1c1b16; --accent: #e8e4da; --accent-fg: #14140f; --warn: #e89b8e;
    --ok: #7fd699;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.5 var(--sans);
}
.wrap { max-width: 46rem; margin-inline: auto; padding: 0 1rem; }
.wrap.wide { max-width: 92rem; }
.masthead { border-bottom: 3px double var(--fg); padding-top: 1.1rem; }
.wordmark {
  font: 700 1.7rem/1.1 var(--serif); letter-spacing: -.015em; text-decoration: none;
  display: inline-block;
}
.nav { display: flex; gap: .15rem 1.1rem; flex-wrap: wrap; margin: .6rem 0 0; }
.nav a {
  padding: .55rem 0 .5rem; text-decoration: none; font-size: .92rem; color: var(--muted);
  border-bottom: 3px solid transparent; margin-bottom: -2px;
}
.nav a[aria-current] { color: var(--fg); font-weight: 650; border-bottom-color: var(--fg); }
.nav a:hover { color: var(--fg); opacity: 1; }
.nav .count { font-size: .78rem; margin-left: .2em; }
main { padding: 1.5rem 0 3.5rem; }
h1 { font: 700 1.7rem/1.2 var(--serif); margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font: 700 1.15rem/1.3 var(--serif); margin: 2.2rem 0 .6rem; }
h3 { font-size: 1rem; margin: 0 0 .2em; }
p { margin: .5em 0; }
a { color: inherit; text-decoration-thickness: .06em; text-underline-offset: .18em; }
a:hover { opacity: .75; }
.small { font-size: .92rem; }
.meta { font-size: .84rem; color: var(--muted); }
.lede { color: var(--muted); margin: 0 0 1rem; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start;
  gap: .75rem 1rem; flex-wrap: wrap; }
.flash { padding: .65rem .85rem; border-radius: 8px; margin: 0 0 1.1rem; font-size: .92rem;
  border: 1px solid; }
.flash.ok { color: var(--ok); }
.flash.err { color: var(--warn); }
.ok-text { color: var(--ok); }
.warn-text { color: var(--warn); }
code, .url {
  font: .84rem/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--soft); padding: .15em .35em; border-radius: 4px; overflow-wrap: anywhere;
}
.url-block { display: block; margin: .45rem 0 0; padding: .6rem .75rem; user-select: all; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.1rem; margin: .7rem 0;
}
.card.lead { border-color: var(--fg); border-width: 1.5px; }
.card > h2:first-child, .card > h3:first-child { margin-top: 0; }
.eyebrow { font-size: .74rem; font-weight: 650; letter-spacing: .09em; text-transform: uppercase;
  color: var(--muted); margin: 0 0 .3rem; }
.headline { font: 700 1.3rem/1.25 var(--serif); margin: 0 0 .2rem; }
.row {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
  gap: .35rem .75rem; padding: .65rem 0; border-top: 1px solid var(--line);
}
.row:first-child { border-top: 0; padding-top: .1rem; }
.row:last-child { padding-bottom: .1rem; }
.row-main { min-width: 0; flex: 1 1 16rem; }
.row-title { display: block; font-size: .97rem; overflow-wrap: anywhere; }
a.row-title { text-decoration: none; font-weight: 550; }
a.row-title:hover { text-decoration: underline; opacity: 1; }
.row-sub { display: block; font-size: .82rem; color: var(--muted); overflow-wrap: anywhere; }
.row-actions { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; }
.row-actions form { margin: 0; }
.tag { font-size: .74rem; font-weight: 600; padding: .1em .5em; border-radius: 99px;
  border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.tag.good { color: var(--ok); border-color: currentColor; }
.tag.bad { color: var(--warn); border-color: currentColor; }
.actions { display: flex; flex-wrap: wrap; gap: .5rem; margin: .9rem 0 0; align-items: center; }
.actions form { margin: 0; }
.btn {
  font: 550 .92rem/1.1 var(--sans); padding: .68em 1em; min-height: 2.6rem;
  border: 1px solid var(--line); border-radius: 7px; background: var(--card);
  color: inherit; cursor: pointer; text-decoration: none;
  display: inline-flex; align-items: center; justify-content: center; text-align: center;
}
.btn:hover { border-color: var(--fg); opacity: 1; }
.btn.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.btn.primary:hover { opacity: .85; }
.btn.tiny { font-size: .82rem; padding: .45em .7em; min-height: 2.1rem; }
.btn.danger { color: var(--warn); border-color: currentColor; }
label { display: block; margin: 1rem 0 .3rem; font-size: .86rem; font-weight: 600; }
label.check { display: flex; gap: .55rem; align-items: flex-start; font-weight: 500; font-size: .95rem; }
label.check input { margin-top: .3rem; width: 1.05rem; height: 1.05rem; flex: none; }
input[type=text], input[type=email], input[type=url], input[type=time], input[type=number],
input[type=password], input[type=file], textarea, select {
  width: 100%; padding: .58rem .65rem; font: inherit; color: inherit; min-height: 2.6rem;
  background: var(--bg); border: 1px solid var(--line); border-radius: 7px;
}
input:focus-visible, select:focus-visible, button:focus-visible, a:focus-visible, summary:focus-visible {
  outline: 2px solid var(--fg); outline-offset: 2px;
}
.hint { font-size: .82rem; color: var(--muted); margin: .3rem 0 0; }
.inline-form { display: flex; flex-wrap: wrap; gap: .5rem; margin: 0; }
.inline-form input:not([type=hidden]), .inline-form select { flex: 1 1 13rem; width: auto; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); gap: 0 1rem; }
.stats { display: flex; flex-wrap: wrap; gap: .75rem 1.75rem; margin: .7rem 0 0; }
.stat strong { display: block; font: 700 1.3rem/1.2 var(--serif); }
.stat span { font-size: .8rem; color: var(--muted); }
.checklist { list-style: none; margin: .4rem 0 0; padding: 0; }
.checklist li { display: flex; gap: .6rem; padding: .4rem 0; align-items: baseline; }
.checklist .mark { flex: none; width: 1.1rem; text-align: center; font-weight: 700; }
.checklist .done { color: var(--muted); text-decoration: line-through; }
details.confirm { display: inline-block; }
details.confirm > summary { list-style: none; cursor: pointer; }
details.confirm > summary::-webkit-details-marker { display: none; }
details.confirm[open] { display: block; flex-basis: 100%; }
.confirm-body { margin-top: .5rem; padding: .7rem .8rem; border: 1px solid var(--line);
  border-radius: 8px; background: var(--soft); font-size: .9rem; }
.confirm-body form { margin: .5rem 0 0; }
.foot { border-top: 1px solid var(--line); padding: 1rem 0 2rem; font-size: .82rem;
  color: var(--muted); display: flex; flex-wrap: wrap; gap: .3rem 1rem; justify-content: space-between; }
.steps { padding-left: 1.2rem; margin: .4rem 0; font-size: .95rem; }
.steps li { margin: .3rem 0; }
.hero { padding: 1.5rem 0 .5rem; }
.hero h1 { font-size: clamp(2rem, 7vw, 2.9rem); line-height: 1.08; margin-bottom: .6rem; }
.hero p { font-size: 1.08rem; max-width: 36rem; }
.article { font: 1.08rem/1.6 var(--serif); }
.article h1, .article h2, .article h3 { margin: 1.6em 0 .4em; font-size: 1.2rem; }
.article p { margin: .9em 0; }
.article img { max-width: 100%; height: auto; border-radius: 6px; }
.article blockquote { margin: 1.2em 0; padding-left: 1em; border-left: 3px solid var(--line);
  color: var(--muted); }
.article pre { overflow-x: auto; font-size: .85rem; }
.article a { text-decoration: underline; }
.tabs { display: flex; gap: 1rem; margin: .8rem 0 0; font-size: .92rem; flex-wrap: wrap; }
.tabs a[aria-current] { font-weight: 700; text-decoration: none; }
.source-filter { display: flex; align-items: baseline; gap: .35rem .9rem;
  flex-wrap: wrap; margin: .55rem 0 0; font-size: .9rem; }
.source-filter .label { font-size: .82rem; color: var(--muted); }
.source-filter a { color: var(--muted); text-decoration: none; }
.source-filter a:hover { color: var(--fg); }
.source-filter a[aria-current] { color: var(--fg); font-weight: 650; }
.bulk-archive { margin: .4rem 0 .2rem; }
.bulk-archive summary { cursor: pointer; font-size: .82rem; }
.bulk-archive .row-actions { display: flex; gap: .5rem; flex-wrap: wrap; }
/* Both panes share the viewport and scroll independently: a mailbox, not a
   page with one box that happens to scroll. */
.reader-split { display: grid; grid-template-columns: minmax(0, 26rem) minmax(0, 1fr);
  gap: 1.2rem; align-items: start; margin-top: .8rem; }
.reader-list, .reader-pane-col {
  max-height: calc(100vh - 15rem); max-height: calc(100dvh - 15rem);
  overflow-y: auto; overscroll-behavior: contain;
}
.reader-list { padding: .2rem .3rem; }
.reader-pane-col { padding-right: .2rem; }
.reader-list .row { padding: .5rem .45rem; border-radius: 8px; }
.reader-list .row.selected { background: var(--soft); box-shadow: inset 3px 0 0 var(--fg); }
/* Mailbox convention: unread is bold and bulleted, read recedes. */
.reader-list .row:not(.read) .row-title { font-weight: 650; }
.reader-list .row:not(.read) .row-title::before {
  content: "\25CF"; font-size: .5em; vertical-align: .28em; margin-right: .45em;
}
.reader-list .row.read .row-main { opacity: .6; }
.reader-list .row.read .row-title { font-weight: 400; }
.reader-list .row.archived .row-main { opacity: .55; }
.reader-list .row.archived .row-title { font-weight: 400; }
/* Queued for the next issue: marked in place. The list never re-sorts on a
   queue click, so the mark (tint, left bar, badge) is what says "included". */
.reader-list .row.queued { background: var(--soft); box-shadow: inset 3px 0 0 var(--muted); }
.reader-list .row.queued.selected { box-shadow: inset 3px 0 0 var(--fg); }
.reader-list .row.queued.read .row-main { opacity: .8; }
.row-badge {
  display: inline-block; margin-left: .45em; padding: 0 .45em;
  border: 1px solid var(--line); border-radius: 999px; background: var(--card);
  font-size: .72rem; font-weight: 600; color: var(--fg); white-space: nowrap;
}
.row-actions { display: flex; gap: .3rem; margin-left: auto; flex-wrap: wrap; }
.row-actions form { margin: 0; }
.row-actions .btn { padding: .15rem .45rem; font-size: .78rem; line-height: 1.4; }
.btn.on { background: var(--soft); border-color: var(--ink); font-weight: 700; }
.article-toolbar { display: flex; gap: .4rem; flex-wrap: wrap; margin: 0 0 1rem; }
.listen { display: flex; gap: .4rem; align-items: center; flex-wrap: wrap; margin: -.4rem 0 1rem; }
.listen select { width: auto; min-height: 0; margin: 0; padding: .2rem .4rem; font-size: .85rem; }
.article .speaking { background: var(--soft); border-radius: 4px; box-shadow: 0 0 0 .3em var(--soft); }
.article-toolbar form { margin: 0; }
.reader-pane .card h1 { margin-top: 0; }
.reader-pane .article { max-width: 42rem; }
.reader-close { display: none; }
@media (max-width: 820px) {
  .reader-split { grid-template-columns: 1fr; }
  .reader-list, .reader-pane-col { max-height: none; overflow: visible; }
  /* A phone has no room beside the list: the article covers it, and closing
     returns to the same spot. Plain links and CSS, like the rest of the reader. */
  .reader-pane-col { display: none; }
  .reader-pane-col.open {
    display: block; position: fixed; inset: 0; z-index: 50; max-height: none;
    overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior: contain;
    background: var(--bg); padding: 0 0 env(safe-area-inset-bottom);
  }
  .reader-pane-col.open .reader-pane { border: 0; border-radius: 0; box-shadow: none; margin: 0; }
  .reader-close {
    display: flex; justify-content: flex-end; position: sticky; top: 0; z-index: 1;
    background: var(--bg); border-bottom: 1px solid var(--line);
    padding: calc(.5rem + env(safe-area-inset-top)) 1rem .5rem;
  }
  body:has(.reader-pane-col.open) { overflow: hidden; }
}
"""

_NAV = (
    ("read", "Reader", "read"),
    ("issues", "Issues", "issues/"),
    ("settings", "Settings", "settings"),
)
# Pages without a tab of their own light up the section they belong to.
_NAV_SECTION = {"next": "issues", "feeds": "settings"}


def _document(title: str, body: str, *, head: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{escape(title)}</title>{head}<style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _public(title: str, body: str, *, head: str = "") -> str:
    """Shell for pages outside a catalogue: landing, sign-in, errors."""
    return _document(
        title,
        f"""<div class="wrap">
<header class="masthead" style="padding-bottom:.7rem"><a class="wordmark" href="/">Matins</a></header>
<main>{body}</main>
<footer class="foot"><span>Your morning paper, on e-ink.</span>
<span><a href="/signin">Sign in</a> · <a href="{REPO_URL}">Source</a></span></footer>
</div>""",
        head=head,
    )


def _shell(
    secret: str,
    active: str,
    title: str,
    body: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    wide: bool = False,
) -> str:
    """The app frame around every catalogue page.

    `wide` is for the Reader: a mailbox needs the screen width, while prose
    pages stay at the reading measure."""
    base = f"/{escape(secret)}/"
    active = _NAV_SECTION.get(active, active)
    links = "".join(
        f'<a href="{base}{path}"{" aria-current=page" if key == active else ""}>{label}</a>'
        for key, label, path in _NAV
    )
    return _document(
        f"{title} · Matins" if title != "Matins" else title,
        f"""<div class="wrap{" wide" if wide else ""}">
<header class="masthead">
  <a class="wordmark" href="{base}">Matins</a>
  <nav class="nav" aria-label="Catalogue">{links}</nav>
</header>
<main>{_flash(notice, error)}{body}</main>
</div>""",
    )


def _flash(notice: str | None, error: str | None) -> str:
    if error:
        return f'<p class="flash err" role="alert">{escape(error)}</p>'
    if notice:
        return f'<p class="flash ok" role="status">{escape(notice)}</p>'
    return ""


def _csrf(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(csrf)}">'


def _button(
    action: str,
    csrf: str,
    label: str,
    *,
    style: str = "",
    fields: dict[str, str] | None = None,
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
        for key, value in (fields or {}).items()
    )
    classes = f"btn {style}".strip()
    return (
        f'<form method="post" action="{escape(action)}">{_csrf(csrf)}{hidden}'
        f'<button class="{classes}" type="submit">{escape(label)}</button></form>'
    )


def _copy_script() -> str:
    """The one script that copies an address; hashed into the page CSP."""
    return f"<script>{COPY_JS}</script>"


def _copy_button(target: str, label: str = "Copy address", *, style: str = "") -> str:
    """Copy the text of `target` (a CSS id) without a form or a round trip."""
    classes = f"btn {style}".strip()
    return (
        f'<button class="{classes}" type="button" data-copy="#{escape(target)}">'
        f"{escape(label)}</button>"
    )


def _confirm(label: str, warning: str, action: str, csrf: str, confirm_label: str) -> str:
    """A destructive action that takes two deliberate clicks, without script."""
    return f"""<details class="confirm">
  <summary class="btn tiny danger">{escape(label)}</summary>
  <div class="confirm-body">{escape(warning)}
    {_button(action, csrf, confirm_label, style="tiny danger")}
  </div>
</details>"""


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


def _ago(timestamp: float | None) -> str:
    if not timestamp:
        return "never"
    seconds = max(0, time() - timestamp)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 129600:
        return _plural(round(seconds / 3600), "hour") + " ago"
    return _plural(round(seconds / 86400), "day") + " ago"


def _when(timestamp: float, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = UTC
    return f"{datetime.fromtimestamp(timestamp, tz):%a %d %b, %H:%M}"


def _article_href(secret: str, article_id: str) -> str:
    return f"/{escape(secret)}/read/{escape(quote(article_id, safe=':-_'))}"


def _edition(issue: dict, timezone: str) -> str:
    return (
        f"{_plural(issue['count'], 'article')} · about {_minutes(issue['words'])}"
        f" · composed {_when(issue['created_at'], timezone)}"
    )


# ------------------------------------------------------------------ home


def _checklist(*, secret: str, feeds: int, issues: int) -> str:
    base = f"/{escape(secret)}"
    steps = [
        (feeds > 0, f'<a href="{base}/feeds">Add your first feed</a> — paste a site or feed address'),
        (issues > 0, f'<a href="{base}/issues/preview">Compose your first issue</a> and check how it reads'),
        (issues > 0, f'<a href="{base}/settings#reader">Add this catalogue to your e-reader</a> to get it there'),
    ]
    items = "".join(
        f'<li><span class="mark">{"✓" if done else "○"}</span>'
        f'<span class="{"done" if done else ""}">{text}</span></li>'
        for done, text in steps
    )
    return f"""<div class="card lead">
  <p class="eyebrow">Getting started</p>
  <ul class="checklist">{items}</ul>
</div>"""


def dashboard_page(
    *,
    secret: str,
    base_url: str,
    csrf: str,
    feeds: list[dict],
    saved_count: int,
    issues: list[dict],
    schedule_label: str,
    timezone: str,
    scheduling: bool,
    plan: dict | None,
    account_email: str | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """The catalogue's home page: what to read now and what comes next."""
    base = f"/{escape(secret)}"
    catalog_url = f"{escape(base_url)}{base}/"
    failing = [feed for feed in feeds if feed["error"]]
    latest = issues[0] if issues else None

    blocks = []
    if not feeds or not issues:
        blocks.append(_checklist(secret=secret, feeds=len(feeds), issues=len(issues)))

    if latest:
        blocks.append(f"""<div class="card lead">
  <p class="eyebrow">Latest issue</p>
  <p class="headline"><a href="{base}/issues/{escape(latest["id"])}" style="text-decoration:none">{escape(latest["title"])}</a></p>
  <p class="meta">{_edition(latest, timezone)}</p>
  <div class="actions">
    <a class="btn primary" href="{base}/issues/{escape(latest["id"])}.epub">Download EPUB</a>
    <a class="btn" href="{base}/issues/{escape(latest["id"])}">See contents</a>
  </div>
</div>""")

    if plan is None:
        contents = '<p class="small">Open the next issue to see what it would contain.</p>'
    elif plan["count"]:
        contents = f"""<div class="stats">
  <div class="stat"><strong>{plan["count"]}</strong><span>{"article" if plan["count"] == 1 else "articles"} waiting</span></div>
</div>"""
    else:
        contents = '<p class="small">Nothing new yet: no fresh feed items and no saved links.</p>'

    if scheduling:
        when = f'<p class="headline">{escape(schedule_label)}</p><p class="meta">Daily, {escape(timezone)} · <a href="{base}/settings#schedule">Change</a></p>'
        toggle = ""
    else:
        when = '<p class="headline">Only when you ask</p><p class="meta">Automatic issues are off.</p>'
        toggle = _button(
            f"{base}/schedule", csrf, "Compose every morning", fields={"enabled": "1"}
        )

    blocks.append(f"""<div class="card">
  <p class="eyebrow">Next issue</p>
  {when}
  {contents}
  <div class="actions">
    <a class="btn{'' if latest else ' primary'}" href="{base}/issues/preview">Review next issue</a>
    {_button(f"{base}/issues/build", csrf, "Compose now")}
    {toggle}
  </div>
</div>""")

    if failing:
        names = ", ".join(escape(feed["title"]) for feed in failing[:3])
        more = f" and {len(failing) - 3} more" if len(failing) > 3 else ""
        blocks.append(f"""<p class="flash err">{_plural(len(failing), "feed")} failing: {names}{more}.
<a href="{base}/feeds">Check your feeds</a></p>""")

    if not account_email:
        blocks.append(f"""<div class="card">
  <h3>Keep this catalogue recoverable</h3>
  <p class="small">No email is attached, so this page's address is the only way in.
  Add one and you can sign in from any device with a one-time code.</p>
  <div class="actions"><a class="btn" href="{base}/settings#account">Add an email</a></div>
</div>""")

    blocks.append(f"""<div class="card">
  <p class="eyebrow">On your e-reader</p>
  <p class="small">Add this address in KOReader, CrossPoint or INX. No username or
  password: the address is the key, so keep it private.</p>
  <span class="url url-block" id="catalog-url">{catalog_url}</span>
  <div class="actions">
    {_copy_button("catalog-url")}
    <a class="btn" href="{base}/settings#reader">Setup steps</a>
  </div>
  <p class="hint" style="margin-bottom:0">Just the issues:
  <span class="url" id="issues-url">{catalog_url}issues/</span>
  {_copy_button("issues-url", "Copy", style="tiny")}</p>
</div>""")

    blocks.append(f"""<h2>Add to the next issue</h2>
<div class="card">
  <label for="save-url" style="margin-top:0">Save an article</label>
  <form class="inline-form" method="post" action="{base}/save">
    <input type="hidden" name="back" value="home">
    <input type="url" id="save-url" name="url" placeholder="https://example.com/article" required>
    <button class="btn" type="submit">Save</button>
  </form>
  <p class="hint">{_plural(saved_count, "saved link")} waiting ·
  {_plural(len(feeds), "feed")} — <a href="{base}/feeds">manage feeds</a></p>
</div>""")

    account = (
        f"Sign-in email: {escape(account_email)} · " if account_email else ""
    )
    blocks.append(
        f'<p class="meta" style="margin-top:2rem">{account}'
        f'<a href="{base}/settings#reader">Connect an e-reader</a></p>'
    )
    return _shell(
        secret,
        "home",
        "Matins",
        "\n".join(blocks) + _copy_script(),
        notice=notice,
        error=error,
    )


# ---------------------------------------------------------------- feeds


def _feed_state(feed: dict) -> str:
    if feed["paused"]:
        return '<span class="tag">Paused</span>'
    if feed["error"]:
        return '<span class="tag bad">Failing</span>'
    if feed["fetched_at"]:
        return '<span class="tag good">OK</span>'
    return '<span class="tag">Not checked</span>'


def _feed_sub(feed: dict) -> str:
    bits = [escape(_host(feed["url"]))]
    if feed["error"]:
        bits.append(f'<span class="warn-text">{escape(feed["error"])}</span>')
    elif feed["fetched_at"]:
        if feed["item_count"] is not None:
            bits.append(_plural(feed["item_count"], "item"))
        if feed["last_item_at"]:
            bits.append(f"latest {_ago(feed['last_item_at'])}")
    return " · ".join(bits)


def feeds_page(
    *,
    secret: str,
    csrf: str,
    feeds: list[dict],
    bluesky: dict,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    rows = "".join(
        f"""<div class="row">
  <div class="row-main">
    <a class="row-title" href="{base}/feeds/{escape(feed["id"])}">{escape(feed["title"])}</a>
    <span class="row-sub">{_feed_sub(feed)}</span>
  </div>
  <div class="row-actions">{_feed_state(feed)}</div>
</div>"""
        for feed in feeds
    )
    listing = (
        f'<div class="card">{rows}</div>'
        if feeds
        else '<div class="card"><p>No feeds yet.</p><p class="meta">Most blogs, '
        "newspapers and newsletters publish one. Paste the site's address above and "
        "Matins will look for it.</p></div>"
    )
    checked = max((feed["fetched_at"] or 0 for feed in feeds), default=0)
    refresh = (
        f"""<div class="actions">
  {_button(f"{base}/feeds/refresh", csrf, "Check all feeds now", style="tiny")}
  <span class="meta">Last checked {_ago(checked)}</span>
</div>"""
        if feeds
        else ""
    )
    posts = int(bluesky.get("posts") or 0)
    accounts = int(bluesky.get("accounts") or 0)
    bluesky_status = (
        f"{_plural(posts, 'post')} from {_plural(accounts, 'account')} · "
        f"{_ago(bluesky.get('last_at') or 0)}"
        if posts
        else "No posts yet — the curator syncs at 06:00."
    )
    body = f"""
<h1>Feeds</h1>
<p class="lede">The sources your issues are made from.</p>

<div class="card">
  <label for="feed-url" style="margin-top:0">Add a feed</label>
  <form class="inline-form" method="post" action="{base}/feeds">
    {_csrf(csrf)}
    <input type="url" id="feed-url" name="url" placeholder="https://example.com" required>
    <button class="btn primary" type="submit">Add</button>
  </form>
  <p class="hint">A feed address or just the site: Matins finds the feed a site advertises.</p>
</div>

<h2>{_plural(len(feeds), "subscription")}</h2>
{listing}
{refresh}

<h2>Curator sources</h2>
<div class="card">
  <div class="row">
    <div class="row-main">
      <span class="row-title">Bluesky</span>
      <span class="row-sub">{bluesky_status}</span>
    </div>
    <div class="row-actions"><a class="btn tiny" href="{base}/read?view=all&source=Bluesky">Open in the Reader</a></div>
  </div>
  <p class="hint">Not an RSS subscription: the 06:00 curator reads the following
  timeline from Bluesky and pushes the posts here as candidates. They are browsable
  and rateable, and printed only when added to an issue.</p>
</div>

<h2>Import and export</h2>
<div class="card">
  <label for="opml" style="margin-top:0">Import from another reader (OPML)</label>
  <form class="inline-form" method="post" action="{base}/feeds/import" enctype="multipart/form-data">
    {_csrf(csrf)}
    <input type="file" id="opml" name="file" accept=".opml,.xml,text/xml,application/xml" required>
    <button class="btn" type="submit">Import</button>
  </form>
  {f'<p class="hint"><a href="{base}/feeds.opml">Export your subscriptions as OPML</a></p>' if feeds else ""}
</div>
"""
    return _shell(secret, "feeds", "Feeds", body, notice=notice, error=error)


_ITEM_STATE = {
    "next": ('<span class="tag good">In next issue</span>'),
    "printed": ('<span class="tag">Already printed</span>'),
    "skipped": ('<span class="tag">Skipped</span>'),
    "old": ('<span class="tag">Too old</span>'),
    "waiting": ('<span class="tag">Over the limit</span>'),
}


def feed_page(
    *,
    secret: str,
    csrf: str,
    feed: dict,
    items: list[dict],
    window_hours: int,
    max_per_feed: int,
    account_voice: str = "af_heart",
    listening: bool = False,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    action = f"{base}/feeds/{escape(feed['id'])}"
    voice_form = (
        f"""<label for="voice">Listening voice</label>
  <form class="inline-form" method="post" action="{action}/voice">
    {_csrf(csrf)}
    <select id="voice" name="voice">{_voice_options(feed.get("voice"), inherit=_voice_label(account_voice))}</select>
    <button class="btn" type="submit">Save</button>
  </form>"""
        if listening
        else ""
    )
    if feed["error"]:
        status = f'<p class="flash err">Last check failed: {escape(feed["error"])}</p>'
    else:
        status = ""
    rows = "".join(
        f"""<div class="row">
  <div class="row-main">
    <a class="row-title" href="{_article_href(secret, item["id"])}">{escape(item["title"])}</a>
    <span class="row-sub">{escape(item["published"])}</span>
  </div>
  <div class="row-actions">{_ITEM_STATE[item["state"]]}</div>
</div>"""
        for item in items
    )
    listing = (
        f'<div class="card">{rows}</div>'
        if items
        else '<div class="card"><p class="meta">Nothing to show: the feed could not be read or is empty.</p></div>'
    )
    pause_label = "Resume" if feed["paused"] else "Pause"
    pause_note = (
        "Paused: still in the reader, left out of issues."
        if feed["paused"]
        else "Pause to keep it in the reader but out of issues."
    )
    body = f"""
<p class="meta"><a href="{base}/feeds">&larr; All feeds</a></p>
<div class="page-head">
  <div><h1>{escape(feed["title"])}</h1>
  <p class="meta" style="overflow-wrap:anywhere">{escape(feed["url"])}</p></div>
  {_feed_state(feed)}
</div>
{status}

<h2>Recent articles</h2>
<p class="meta">An issue takes items from the last {_plural(window_hours, "hour")},
at most {max_per_feed} per feed.</p>
{listing}

<h2>Manage</h2>
<div class="card">
  <label for="title" style="margin-top:0">Name in your issues</label>
  <form class="inline-form" method="post" action="{action}/rename">
    {_csrf(csrf)}
    <input type="text" id="title" name="title" value="{escape(feed["title"])}" maxlength="120" required>
    <button class="btn" type="submit">Rename</button>
  </form>
  {voice_form}
  <div class="actions">
    {_button(f"{action}/pause", csrf, pause_label, fields={"paused": "0" if feed["paused"] else "1"})}
    <span class="meta">{pause_note}</span>
  </div>
  <div class="actions">
    {_confirm("Unsubscribe", "Stop following this feed. Issues already composed keep their articles.", f"{action}/delete", csrf, "Yes, unsubscribe")}
  </div>
</div>

<h2>Subscriber access</h2>
<div class="card">
  <p class="hint">For paywalled publishers: paste the <code>Cookie</code> request header from a
  logged-in browser tab. It is stored encrypted and never shown again; it is used only
  when fetching this feed's article pages, and only on the same site after redirects.</p>
  <form method="post" action="{action}/auth">
    {_csrf(csrf)}
    <label for="cookie">Cookie</label>
    <input type="password" id="cookie" name="cookie" autocomplete="off"
      placeholder="{escape('A cookie is set — paste a new one to replace it' if feed['auth_cookie_set'] else 'lm_session=...; other=...')}">
    <div class="actions"><button class="btn" type="submit">Save cookie</button></div>
  </form>
  <div class="actions">
    {_button(f"{action}/auth", csrf, "Remove", style="tiny", fields={"cookie": "-"}) if feed["auth_cookie_set"] else ""}
  </div>
</div>
"""
    return _shell(secret, "feeds", feed["title"], body, notice=notice, error=error)


# ------------------------------------------------------------ next issue


def preview_page(
    *,
    secret: str,
    csrf: str,
    candidates: list[dict],
    skipped: list[dict],
    window_hours: int,
    max_articles: int,
    max_per_feed: int,
    max_words: int,
    schedule_label: str | None,
    has_feeds: bool,
    curated: bool = False,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """The next issue as something to edit, before anything is written."""
    base = f"/{escape(secret)}"
    add_link = f"""<div class="card">
  <label for="save-url" style="margin-top:0">Add an article to this issue</label>
  <form class="inline-form" method="post" action="{base}/save">
    <input type="hidden" name="back" value="next">
    <input type="url" id="save-url" name="url" placeholder="https://example.com/article" required>
    <button class="btn" type="submit">Add</button>
  </form>
</div>"""

    skipped_block = ""
    if skipped and not curated:
        rows = "".join(
            f"""<div class="row">
  <div class="row-main"><span class="row-title">{escape(item["title"])}</span>
  <span class="row-sub">{escape(item["section"])}</span></div>
  <div class="row-actions">{_button(f"{base}/issues/skip/undo", csrf, "Put back", style="tiny", fields={"article": item["id"]})}</div>
</div>"""
            for item in skipped
        )
        skipped_block = f'<h2>Skipped</h2><div class="card">{rows}</div>'

    when = (
        f"Next scheduled: {escape(schedule_label)}."
        if schedule_label
        else f'Automatic issues are off — <a href="{base}/settings#schedule">turn them on</a>.'
    )
    header = f"""<h1>Next issue</h1>
<p class="lede">{when}</p>"""

    if not candidates:
        if curated:
            body = f"""{header}
<div class="card">
  <p>Nothing curated yet.</p>
  <p class="meta">This catalogue prints only what you add. Open the Reader, read
  around, and use <strong>+ Next</strong> (or the curator's picks) to fill the issue.</p>
  <div class="actions"><a class="btn primary" href="{base}/read">Open the Reader</a></div>
</div>
{add_link}
"""
            return _shell(secret, "next", "Next issue", body, notice=notice, error=error)
        target = (
            f'<a class="btn" href="{base}/feeds">Manage feeds</a>'
            if has_feeds
            else f'<a class="btn primary" href="{base}/feeds">Add a feed</a>'
        )
        body = f"""{header}
<div class="card">
  <p>Nothing is queued.</p>
  <p class="meta">An issue is built from feed items published in the last
  {_plural(window_hours, "hour")} that have not already been printed, plus anything
  you save.</p>
  <div class="actions">{target}</div>
</div>
{add_link}
{skipped_block}"""
        return _shell(secret, "next", "Next issue", body, notice=notice, error=error)

    sections: dict[str, list[dict]] = {}
    for candidate in candidates:
        sections.setdefault(candidate["section"], []).append(candidate)

    blocks = ""
    for section, items in sections.items():
        rows = ""
        for item in items:
            if item["saved_id"] is not None:
                action = _button(
                    f"{base}/saved/{item['saved_id']}/delete",
                    csrf,
                    "Remove",
                    style="tiny",
                    fields={"back": "next"},
                )
            else:
                action = _button(
                    f"{base}/issues/skip",
                    csrf,
                    "Skip",
                    style="tiny",
                    fields={
                        "article": item["id"],
                        "title": item["title"],
                        "section": section,
                        "url": item["url"] or "",
                    },
                )
            rows += f"""<div class="row">
  <div class="row-main">
    <a class="row-title" href="{_article_href(secret, item["id"])}">{escape(item["title"])}</a>
    {f'<span class="row-sub">about {_minutes(item["words"])}</span>' if item["words"] else ""}
  </div>
  <div class="row-actions">{action}</div>
</div>"""
        blocks += f'<h2>{escape(section)}</h2><div class="card">{rows}</div>'

    limits_hint = (
        "Only the articles you added are printed; feeds are for browsing in the Reader."
        if curated
        else (
            f"Limits: {max_per_feed} per feed, {max_words:,} words, items from the "
            f'last {_plural(window_hours, "hour")} — '
            f'<a href="{base}/settings#limits">change</a>. '
            "Skip an article and the next in line takes its place."
        )
    )
    body = f"""{header}
<div class="card lead">
  <div class="stats" style="margin-top:0">
    <div class="stat"><strong>{len(candidates)}</strong><span>{"article" if len(candidates) == 1 else "articles"}, limit {max_articles}</span></div>
    <div class="stat"><strong>{len(sections)}</strong><span>{"source" if len(sections) == 1 else "sources"}</span></div>
  </div>
  <div class="actions">
    {_button(f"{base}/issues/build", csrf, "Compose this issue", style="primary")}
  </div>
  <p class="hint">{limits_hint}
  Reading time is known once composed.</p>
</div>

{blocks}

{add_link}
{skipped_block}
"""
    return _shell(secret, "next", "Next issue", body, notice=notice, error=error)


def composing_page(*, secret: str) -> str:
    """Shown while a slow build runs. A meta refresh is the no-script way to poll."""
    base = f"/{escape(secret)}"
    page = _shell(
        secret,
        "next",
        "Composing",
        """<h1>Composing your issue…</h1>
<div class="card"><p>Fetching each article and its images. This page moves on by
itself as soon as the EPUB is ready.</p>
<p class="meta">You can leave: the issue will be waiting under Issues.</p></div>""",
    )
    return page.replace(
        "</title>",
        f'</title><meta http-equiv="refresh" content="3;url={base}/issues/composing">',
        1,
    )


# --------------------------------------------------------------- issues


def issues_page(
    *,
    secret: str,
    csrf: str,
    issues: list[dict],
    timezone: str,
    retention: int,
    schedule_label: str | None,
    queued: int,
    failing: list[str],
    notice: str | None = None,
    error: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    rows = "".join(
        f"""<div class="row">
  <div class="row-main">
    <a class="row-title" href="{base}/issues/{escape(issue["id"])}">{escape(issue["title"])}</a>
    <span class="row-sub">{_edition(issue, timezone)}</span>
  </div>
  <div class="row-actions"><a class="btn tiny{" primary" if i == 0 else ""}" href="{base}/issues/{escape(issue["id"])}.epub">Download</a></div>
</div>"""
        for i, issue in enumerate(issues)
    )
    if rows:
        listing = f'<div class="card">{rows}</div>'
    else:
        listing = f"""<div class="card"><p>No issues yet.</p>
<div class="actions"><a class="btn primary" href="{base}/issues/preview">Review the next issue</a></div></div>"""
    keeps = f"The latest {retention} are kept." if retention else ""
    when = (
        escape(schedule_label)
        if schedule_label
        else f'Automatic issues are off · <a href="{base}/settings#schedule">Change</a>'
    )
    trouble = ""
    if failing:
        names = ", ".join(escape(name) for name in failing[:3])
        more = f" and {len(failing) - 3} more" if len(failing) > 3 else ""
        trouble = (
            f'<p class="flash err">{_plural(len(failing), "feed")} failing: {names}{more}. '
            f'<a href="{base}/feeds">Check your feeds</a></p>'
        )
    body = f"""
<h1>Issues</h1>
{trouble}
<div class="card lead">
  <p class="eyebrow">Next issue</p>
  <p class="headline">{when}</p>
  <p class="meta">{_plural(queued, "article")} queued</p>
  <div class="actions">
    <a class="btn" href="{base}/issues/preview">Review</a>
    {_button(f"{base}/issues/build", csrf, "Compose now")}
  </div>
</div>
{listing}
<p class="hint">{keeps}</p>
"""
    return _shell(secret, "issues", "Issues", body, notice=notice, error=error)


def issue_page(
    *,
    secret: str,
    csrf: str,
    issue: dict,
    entries: list[dict],
    timezone: str,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    sections: dict[str, list[dict]] = {}
    for entry in entries:
        sections.setdefault(entry["section"], []).append(entry)
    blocks = ""
    for section, items in sections.items():
        rows = "".join(
            f"""<div class="row"><div class="row-main">{
                f'<a class="row-title" href="{escape(item["url"])}" target="_blank" rel="noopener noreferrer">{escape(item["title"])}</a>'
                if item["url"]
                else f'<span class="row-title">{escape(item["title"])}</span>'
            }</div></div>"""
            for item in items
        )
        blocks += f'<h2>{escape(section)}</h2><div class="card">{rows}</div>'
    body = f"""
<p class="meta"><a href="{base}/issues/">&larr; All issues</a></p>
<h1>{escape(issue["title"])}</h1>
<p class="lede">{_edition(issue, timezone)}</p>
<div class="actions" style="margin-bottom:1rem">
  <a class="btn primary" href="{base}/issues/{escape(issue["id"])}.epub">Download EPUB</a>
  <a class="btn" href="{base}/read?issue={escape(issue["id"])}">See in reader</a>
</div>
{blocks}
<div class="actions" style="margin-top:2rem">
  {_confirm(
      "Delete this issue",
      "The EPUB is removed and its articles go back in line for the next issue, if still recent enough. If today's scheduled time has passed, a new issue may be composed within a minute.",
      f"{base}/issues/{issue['id']}/delete",
      csrf,
      "Yes, delete it",
  )}
</div>
"""
    return _shell(secret, "issues", issue["title"], body, notice=notice, error=error)


# ------------------------------------------------------------- settings


@lru_cache(maxsize=1)
def _voice_options(current: str | None, *, inherit: str | None = None) -> str:
    """Grouped by language; `inherit` adds a first "use the default" choice."""
    from .voices import VOICES

    groups: dict[str, list] = {}
    for voice in VOICES:
        groups.setdefault(voice.language, []).append(voice)
    head = (
        f'<option value=""{" selected" if not current else ""}>Catalogue voice ({escape(inherit)})</option>'
        if inherit is not None
        else ""
    )
    return head + "".join(
        f'<optgroup label="{escape(language)}">'
        + "".join(
            f'<option value="{voice.id}"{" selected" if voice.id == current else ""}>'
            f"{escape(voice.name)}</option>"
            for voice in members
        )
        + "</optgroup>"
        for language, members in groups.items()
    )


def _voice_label(voice_id: str) -> str:
    from .voices import VOICES

    voice = next((v for v in VOICES if v.id == voice_id), None)
    return f"{voice.name}, {voice.language}" if voice else voice_id


def _timezone_options() -> str:
    names = sorted(name for name in available_timezones() if "/" in name and not name.startswith("Etc/"))
    return "".join(f'<option value="{escape(name)}">' for name in names)


def _number(name: str, label: str, value, default: int, low: int, high: int) -> str:
    return f"""<div>
  <label for="{name}">{escape(label)}</label>
  <input type="number" id="{name}" name="{name}" min="{low}" max="{high}"
    value="{escape(str(value)) if value is not None and value != "" else ""}" placeholder="{default}">
</div>"""


def _listening_card(voice: str) -> str:
    return f"""
  <h2 id="listening">Listening</h2>
  <div class="card">
    <label for="voice" style="margin-top:0">Voice</label>
    <select id="voice" name="voice">{_voice_options(voice)}</select>
    <p class="hint">Reads an article aloud from the Reader's ▶ Listen button. The voice
    runs in your browser: the first time it downloads about 90 MB (330 MB on a computer
    with a graphics card), then nothing leaves your device. A feed can have its own
    voice, for a feed in another language.</p>
  </div>
"""


def settings_page(
    *,
    secret: str,
    base_url: str,
    csrf: str,
    email: str,
    webhook_url: str,
    schedule: str,
    timezone: str,
    enabled: bool,
    auto_select: bool,
    show_dates: bool,
    webhook_secret_set: bool,
    voice: str = "af_heart",
    listening: bool = False,
    limits: dict,
    defaults: dict,
    bounds: dict,
    schedule_label: str | None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    catalog_url = f"{escape(base_url)}{base}/"
    secret_note = (
        "A signing secret is set. Leave blank to keep it, or enter - to remove it."
        if webhook_secret_set
        else "Optional. The receiver uses it to verify the signature."
    )
    next_line = (
        f'<p class="hint">Next: {escape(schedule_label)}.</p>' if schedule_label else ""
    )
    limit_fields = "".join(
        _number(key, label, limits.get(key), defaults[key], *bounds[key])
        for key, label in (
            ("max_articles", "Articles per issue"),
            ("max_saved", "Queued articles per issue"),
            ("max_per_feed", "Articles per feed"),
            ("max_words", "Words per issue"),
            ("window_hours", "Look back (hours)"),
        )
    )
    body = f"""
<h1>Settings</h1>

<div class="card">
  <div class="row"><div class="row-main">
    <a class="row-title" href="{base}/feeds">Feeds</a>
    <span class="row-sub">What you can browse in the Reader.</span>
  </div></div>
</div>

<h2 id="send-to">Send to Matins</h2>
<div class="card">
  <p class="small">Adds a share-sheet action on iPhone and iPad: share any link to your next
  issue. The button copies your address; paste it when Shortcuts asks.</p>
  <span class="url url-block" id="send-address">{catalog_url}</span>
  <div class="actions">
    <a class="btn primary" data-copy="#send-address" href="/assets/send-to-matins.shortcut">Add the Shortcut</a>
  </div>
</div>

<form method="post" action="{base}/settings" id="settings">
  {_csrf(csrf)}

  <h2 id="schedule" style="margin-top:1.2rem">Schedule</h2>
  <div class="card">
    <label class="check" style="margin-top:0">
      <input type="checkbox" name="enabled" value="1" {"checked" if enabled else ""}>
      <span>Compose an issue automatically every day</span>
    </label>
    <div class="grid2">
      <div><label for="schedule-time">At</label>
        <input type="time" id="schedule-time" name="schedule" value="{escape(schedule)}"></div>
      <div><label for="timezone">Timezone</label>
        <input type="text" id="timezone" name="timezone" value="{escape(timezone)}"
          list="timezones" placeholder="Europe/Paris" autocomplete="off">
        <datalist id="timezones">{_timezone_options()}</datalist></div>
    </div>
    {next_line}
  </div>

  <h2 id="limits">What goes in an issue</h2>
  <div class="card">
    <label for="issue-mode" style="margin-top:0">Issue sources</label>
    <select id="issue-mode" name="auto_select">
      <option value="1" {"selected" if auto_select else ""}>Automatic — newest from the feeds, plus the queue</option>
      <option value="0" {"selected" if not auto_select else ""}>Curated — only what I add (the next-issue queue)</option>
    </select>
    <p class="hint">Curated keeps the feeds for browsing in the Reader; an issue is
    only what you or the curator put in the queue.</p>
    <label class="check">
      <input type="checkbox" name="show_dates" value="1" {"checked" if show_dates else ""}>
      <span>Show publication dates in the Reader and in issues</span>
    </label>
    <div class="grid2">{limit_fields}</div>
    <p class="hint">Blank uses the default shown. Nothing is printed twice.</p>
  </div>
{_listening_card(voice) if listening else f'<input type="hidden" name="voice" value="{escape(voice)}">'}

  <details id="account"{" open" if not email else ""}>
  <summary><h2 style="display:inline">Account and notifications</h2></summary>
  <div class="card">
    <label for="email" style="margin-top:0">Email</label>
    <input type="email" id="email" name="email" value="{escape(email)}" placeholder="you@example.com">
    <p class="hint">Opens this catalogue from <a href="/signin">any device</a> with a one-time
    code, and receives a short note when an issue is ready. One address opens one catalogue.
    Clearing it removes sign-in by email.</p>
    <label for="webhook_url">Webhook</label>
    <input type="url" id="webhook_url" name="webhook_url" value="{escape(webhook_url)}"
      placeholder="https://example.com/hooks/matins">
    <p class="hint">Called with <code>issue.ready</code> for each new issue. Optional.</p>
    <label for="webhook_secret">Webhook signing secret</label>
    <input type="text" id="webhook_secret" name="webhook_secret" value="" autocomplete="off"
      placeholder="{escape("unchanged" if webhook_secret_set else "optional")}">
    <p class="hint">{escape(secret_note)} Signed <code>t=&lt;unix&gt;,v1=&lt;hmac-sha256&gt;</code>
    over <code>timestamp.body</code> in <code>X-Matins-Signature</code>.</p>
    <div class="actions">
      {'<button class="btn tiny" type="submit" form="test-email">Send a test email</button>' if email else ""}
      {'<button class="btn tiny" type="submit" form="test-webhook">Send a test webhook</button>' if webhook_url else ""}
    </div>
  </div>
  </details>

  <div class="actions"><button class="btn primary" type="submit">Save settings</button></div>
</form>
<form id="test-email" method="post" action="{base}/settings/test-email">{_csrf(csrf)}</form>
<form id="test-webhook" method="post" action="{base}/settings/test-webhook">{_csrf(csrf)}</form>

<h2 id="reader">Reader</h2>
<div class="card">
  <p class="small">Add this address to KOReader, CrossPoint or any OPDS reader. No username
  or password: the address is the key, so keep it private.</p>
  <span class="url url-block" id="catalog-url">{catalog_url}</span>
  <div class="actions">{_copy_button("catalog-url")}</div>
  <ol class="steps">
    <li>In KOReader: search icon &rarr; <strong>OPDS catalog</strong> &rarr; <strong>+</strong>.</li>
    <li>Paste the address above and save.</li>
    <li>Open <strong>Issues</strong> for each morning's paper.</li>
  </ol>
  <p class="hint">Just the issues:
  <code id="issues-url" style="user-select:all">{catalog_url}issues/</code>
  {_copy_button("issues-url", "Copy", style="tiny")}</p>
</div>

<details id="access">
<summary><h2 style="display:inline">Access</h2></summary>
<div class="card">
  <div class="row"><div class="row-main">
    <span class="row-title">Get a new address</span>
    <span class="row-sub">If this one leaked. Your feeds and issues move over; every device must be updated.</span>
  </div><div class="row-actions">
    {_confirm("New address", "The current address stops working immediately.", f"{base}/regenerate", csrf, "Yes, change it")}
  </div></div>
  <div class="row"><div class="row-main">
    <span class="row-title">Forget on this device</span>
    <span class="row-sub">Removes it from this browser's list. The catalogue keeps working.</span>
  </div><div class="row-actions">
    {_button(f"{base}/forget", csrf, "Forget", style="tiny")}
  </div></div>
  <div class="row"><div class="row-main">
    <span class="row-title">Delete this catalogue</span>
    <span class="row-sub">Feeds, saved links, issues and settings, for good.</span>
  </div><div class="row-actions">
    {_confirm("Delete", "This cannot be undone.", f"{base}/delete", csrf, "Yes, delete everything")}
  </div></div>
</div>
</details>
<p class="hint"><a href="/?about=1">About Matins</a></p>
"""
    return _shell(
        secret,
        "settings",
        "Settings",
        body + _copy_script(),
        notice=notice,
        error=error,
    )


# --------------------------------------------------------------- reader


# Progressive enhancement, not a dependency: without it the reader still works,
# the list just jumps to the top after a click. A full-page navigation discards
# both the inner list's scrollTop and the page scroll, so save them per tab and
# restore on load. Kept as a constant because its hash is in the reader's CSP.
READER_SCROLL_JS = (
    "(function(){var l=document.querySelector('.reader-list');if(!l)return;"
    "var k='matins-scroll:'+location.pathname;"
    "if('scrollRestoration' in history)history.scrollRestoration='manual';"
    "function restore(){try{var y=sessionStorage.getItem(k);"
    "if(y!==null)l.scrollTop=+y;var p=sessionStorage.getItem(k+':page');"
    "if(p!==null)window.scrollTo(0,+p);}catch(e){}}"
    "function save(){try{sessionStorage.setItem(k,l.scrollTop);"
    "sessionStorage.setItem(k+':page',window.scrollY);}catch(e){}}"
    "window.addEventListener('pagehide',save);"
    "l.addEventListener('click',save);l.addEventListener('submit',save);"
    "restore();window.addEventListener('load',restore);}());"
)

# Like, + Next, Archive and the pane's buttons post in the background and patch
# the rows and toolbar from the page the redirect lands on, so a tap does not
# reload (and flash) the whole reader. Without fetch the forms post as before;
# anything that changes the view or the open article still navigates.
READER_ACTIONS_JS = """(function(){
var split=document.querySelector('.reader-split');
if(!split||!window.fetch||!window.DOMParser||!window.URLSearchParams)return;
function q(u,k){return new URL(u,location.href).searchParams.get(k)||'';}
function rowsFor(root,id){return Array.prototype.filter.call(
  root.querySelectorAll('.row[data-id]'),function(r){return r.dataset.id===id;});}
split.addEventListener('submit',function(ev){
  var f=ev.target,row=f.closest('.row[data-id]'),pane=f.closest('.reader-pane');
  var idField=f.querySelector('input[name=article_id]');
  if((!row&&!pane)||!idField)return;
  ev.preventDefault();
  var id=idField.value,buttons=f.querySelectorAll('button');
  buttons.forEach(function(b){b.disabled=true;});
  fetch(f.action,{method:'POST',body:new URLSearchParams(new FormData(f)),
    credentials:'same-origin',headers:{'Accept':'text/html'}})
  .then(function(r){if(!r.ok||r.redirected===false)throw r;
    return r.text().then(function(t){return {url:r.url,html:t};});})
  .then(function(res){
    var moved=q(res.url,'view')!==q(location.href,'view')||
      (pane&&q(res.url,'article')!==q(location.href,'article'));
    if(moved){location.href=res.url;return;}
    try{patch(res);}catch(e){location.href=res.url;}
  },function(){buttons.forEach(function(b){b.disabled=false;});f.submit();});
  function patch(res){
    var doc=new DOMParser().parseFromString(res.html,'text/html');
    var fresh=rowsFor(doc,id)[0];
    rowsFor(document,id).forEach(function(old){
      if(fresh)old.replaceWith(document.importNode(fresh,true));else old.remove();});
    var bar=document.querySelector('.reader-pane .article-toolbar'),
        nbar=doc.querySelector('.reader-pane .article-toolbar');
    if(bar&&nbar&&q(location.href,'article')===id)
      bar.replaceWith(document.importNode(nbar,true));
  }
});
}());"""


# Listen: reads the open article aloud, synthesised in the reader's own browser
# by Kokoro in a worker (/assets/tts/worker.js). The page pulls audio one chunk
# at a time, a few ahead of what is playing, so a long article never piles up in
# memory and the first sentence plays as soon as it is ready. Clicking a
# paragraph while listening jumps there. Delegated from the document, because the
# background row actions replace the toolbar's buttons after every tap.
READER_LISTEN_JS = """(function(){
var box=document.querySelector('.reader-pane .listen');
if(!box||!window.Worker||!(window.AudioContext||window.webkitAudioContext))return;
box.hidden=false;
var button=box.querySelector('[data-listen]'),stopper=box.querySelector('[data-stop]'),
    status=box.querySelector('[data-status]'),speed=box.querySelector('[data-speed]');
var AHEAD=3,worker=null,ctx=null,run=0,blocks=[],pending=0,ended=false,
    playing=false,nextAt=0,sources=[],current=-1;
try{var saved=localStorage.getItem('matins-listen-speed');if(saved)speed.value=saved;}catch(e){}
function say(t){status.textContent=t;}
function texts(){
  var root=document.querySelector('.reader-pane .article'),sel='p,h1,h2,h3,h4,h5,h6,li,blockquote,figcaption';
  return Array.prototype.filter.call(root.querySelectorAll(sel),function(el){
    return !el.querySelector(sel)&&!el.closest('pre')&&el.textContent.trim();});
}
function mark(i){
  if(current>=0&&blocks[current])blocks[current].classList.remove('speaking');
  current=i;
  if(i>=0&&blocks[i]){blocks[i].classList.add('speaking');
    blocks[i].scrollIntoView({block:'nearest',behavior:'smooth'});}
}
function request(){while(!ended&&pending<AHEAD){pending++;
  worker.postMessage({type:'next',run:run,speed:+speed.value});}}
function start(from){
  run++;pending=0;ended=false;
  sources.forEach(function(s){try{s.onended=null;s.stop();}catch(e){}});sources=[];
  if(!ctx)ctx=new (window.AudioContext||window.webkitAudioContext)();
  ctx.resume();nextAt=ctx.currentTime;playing=true;
  button.textContent='❚❚ Pause';stopper.hidden=false;
  worker.postMessage({type:'speak',run:run,voice:box.dataset.voice,
    blocks:blocks.slice(from).map(function(el){return el.textContent.replace(/\s+/g,' ').trim();}),
    offset:from});
  request();
}
function stop(){
  run++;ended=true;playing=false;
  sources.forEach(function(s){try{s.onended=null;s.stop();}catch(e){}});sources=[];
  if(ctx)ctx.suspend();mark(-1);button.textContent='▶ Listen';stopper.hidden=true;say('');
}
function onMessage(ev){
  var m=ev.data;
  if(m.type==='status'){say(m.text);return;}
  if(m.type==='error'){stop();say(m.text);return;}
  if(m.run!==run)return;
  if(m.type==='end'){ended=true;pending=0;
    var last=sources[sources.length-1];
    if(last)last.addEventListener('ended',function(){if(m.run===run)stop();});else stop();
    return;}
  if(m.type!=='audio')return;
  pending--;
  var buffer=ctx.createBuffer(1,m.samples.length,m.rate);
  buffer.copyToChannel(m.samples,0);
  var src=ctx.createBufferSource();src.buffer=buffer;src.connect(ctx.destination);
  var at=Math.max(nextAt,ctx.currentTime+0.05);src.start(at);nextAt=at+buffer.duration;
  sources.push(src);
  var block=m.block,wait=Math.max(0,(at-ctx.currentTime)*1000),mine=run;
  setTimeout(function(){if(mine===run&&playing)mark(block);},wait);
  src.onended=function(){sources.splice(sources.indexOf(src),1);if(mine===run)request();};
  say('');
}
document.addEventListener('click',function(ev){
  if(ev.target.closest('[data-stop]')){stop();return;}
  if(ev.target.closest('[data-listen]')){
    if(!worker){worker=new Worker(box.dataset.worker,{type:'module'});
      worker.onmessage=onMessage;
      worker.onerror=function(){stop();say('Listening is not available in this browser.');};}
    if(!blocks.length)blocks=texts();
    if(!blocks.length){say('Nothing to read.');return;}
    if(sources.length||pending){
      if(playing){ctx.suspend();playing=false;button.textContent='▶ Resume';}
      else{ctx.resume();playing=true;button.textContent='❚❚ Pause';}
      return;}
    start(Math.max(0,current));return;}
  var para=worker&&!stopper.hidden&&ev.target.closest('.reader-pane .article *');
  if(para&&!ev.target.closest('a')){
    var i=blocks.findIndex(function(el){return el===para||el.contains(para);});
    if(i>=0){mark(i);start(i);}
  }
});
speed.addEventListener('change',function(){
  try{localStorage.setItem('matins-listen-speed',speed.value);}catch(e){}});
window.addEventListener('pagehide',function(){if(worker)worker.terminate();});
}());"""


_READER_VIEWS = (
    ("all", "All"),
    ("unread", "Unread"),
    ("liked", "Liked"),
    ("queued", "Next issue"),
    ("archived", "Archived"),
)


def _reader_href(
    secret: str,
    view: str,
    article: str | None = None,
    source: str | None = None,
    issue: str | None = None,
) -> str:
    base = f"/{escape(secret)}/read?view={quote(view)}"
    if source:
        base += f"&source={quote(source)}"
    if issue:
        base += f"&issue={quote(issue)}"
    if article:
        base += f"&article={quote(article, safe='')}"
    return base


def _reader_row(
    secret: str,
    csrf: str,
    item: dict,
    view: str,
    source: str | None,
    issue: str | None,
    selected_id: str = "",
) -> str:
    """One list row: open it, like it, queue it, or discard it.

    The row's buttons keep whatever article is open (none on a phone list), so
    liking or queuing from the list never pops the article overlay open."""
    if item.get("archived"):
        restore = _button(
            f"/{escape(secret)}/read/restore",
            csrf,
            "Restore",
            style="tiny",
            fields={
                "article_id": item["id"],
                "view": "archived",
                "source": source or "",
                "issue": issue or "",
            },
        )
        return f"""<div class="row archived" data-id="{escape(item["id"])}">
  <div class="row-main">
    <span class="row-title">{escape(item["title"])}</span>
    <span class="row-sub">{escape(item["source"])} · archived</span>
  </div>
  <div class="row-actions">{restore}</div>
</div>"""
    liked = item["rating"] > 0
    queued = item["saved"]
    like = _button(
        f"/{escape(secret)}/read/feedback",
        csrf,
        "♥" if liked else "♡",
        style="tiny on" if liked else "tiny",
        fields={
            "article_id": item["id"],
            "value": "clear" if liked else "like",
            "selected": selected_id,
            "from_list": "1",
            "view": view,
            "source": source or "",
            "issue": issue or "",
        },
    )
    queue = _button(
        f"/{escape(secret)}/read/{'unqueue' if queued else 'queue'}",
        csrf,
        "✓ Next" if queued else "+ Next",
        style="tiny on" if queued else "tiny",
        fields={
            "article_id": item["id"],
            "selected": selected_id,
            "from_list": "1",
            "view": view,
            "source": source or "",
            "issue": issue or "",
        },
    )
    archive = _button(
        f"/{escape(secret)}/read/archive",
        csrf,
        "Archive",
        style="tiny",
        fields={
            "article_id": item["id"],
            "view": view,
            "source": source or "",
            "issue": issue or "",
        },
    )
    selected = " selected" if item.get("selected") else ""
    read = " read" if item["read"] else ""
    queued = " queued" if item["saved"] else ""
    badge = '<span class="row-badge">Next issue</span>' if item["saved"] else ""
    timing = f" · {_minutes(item['minutes'] * 250)}" if item["minutes"] else ""
    date = f" · {escape(item['published'])}" if item.get("published") else ""
    return f"""<div class="row{selected}{read}{queued}" data-id="{escape(item["id"])}">
  <div class="row-main">
    <a class="row-title" href="{_reader_href(secret, view, item["id"], source, issue)}">{escape(item["title"])}</a>
    <span class="row-sub">{escape(item["source"])}{date}{timing}{badge}</span>
  </div>
  <div class="row-actions">{like}{queue}{archive}</div>
</div>"""


def _reader_pane(
    *,
    secret: str,
    csrf: str,
    view: str,
    source: str | None,
    issue: str | None,
    selected: dict,
    body: str,
    voice: str | None = None,
) -> str:
    base = f"/{escape(secret)}"
    rating = selected["rating"]
    original = (
        f'<a class="btn tiny" href="{base}/read/open?article='
        f'{quote(selected["id"], safe="")}" target="_blank" '
        'rel="noopener noreferrer">Open original</a>'
        if selected["url"]
        else ""
    )
    queue = _button(
        f"{base}/read/{'unqueue' if selected['saved'] else 'queue'}",
        csrf,
        "✓ In next issue" if selected["saved"] else "+ Add to next issue",
        style="tiny on" if selected["saved"] else "tiny",
        fields={"article_id": selected["id"], "view": view, "source": source or "", "issue": issue or ""},
    )
    like = _button(
        f"{base}/read/feedback",
        csrf,
        "♥ Liked" if rating > 0 else "♡ Like",
        style="tiny on" if rating > 0 else "tiny",
        fields={
            "article_id": selected["id"],
            "value": "clear" if rating > 0 else "like",
            "view": view,
            "source": source or "",
            "issue": issue or "",
        },
    )
    dislike = _button(
        f"{base}/read/feedback",
        csrf,
        "Not for me",
        style="tiny on" if rating < 0 else "tiny",
        fields={
            "article_id": selected["id"],
            "value": "clear" if rating < 0 else "dislike",
            "view": view,
            "source": source or "",
            "issue": issue or "",
        },
    )
    archive = _button(
        f"{base}/read/archive",
        csrf,
        "Archive",
        style="tiny",
        fields={"article_id": selected["id"], "view": view, "source": source or "", "issue": issue or ""},
    )
    unread = _button(
        f"{base}/read/{quote(selected['id'], safe=':-_')}/unread",
        csrf,
        "Mark unread",
        style="tiny",
        fields={"view": view, "source": source or "", "issue": issue or ""},
    )
    timing = f" · {_minutes(selected['minutes'] * 250)}" if selected["minutes"] else ""
    date = f" · {escape(selected['published'])}" if selected.get("published") else ""
    return f"""<div class="card reader-pane">
<h1>{escape(selected["title"])}</h1>
<p class="meta" style="margin-top:-.4rem">{escape(selected["source"])}{date}{timing}</p>
<div class="article-toolbar">{like}{dislike}{queue}{archive}{unread}{original}</div>
{_listen_bar(voice)}
<div class="article">{body}</div>
</div>"""


def _listen_bar(voice: str | None) -> str:
    """The player, hidden until its script finds a browser that can run it."""
    if not voice:
        return ""
    speeds = "".join(
        f'<option value="{value}"{" selected" if value == "1" else ""}>{label}</option>'
        for value, label in (("0.8", "0.8×"), ("1", "1×"), ("1.2", "1.2×"), ("1.4", "1.4×"))
    )
    return (
        f'<div class="listen" hidden data-voice="{escape(voice)}" data-worker="/assets/tts/worker.js">'
        '<button class="btn tiny" type="button" data-listen>▶ Listen</button>'
        '<button class="btn tiny" type="button" data-stop hidden>■ Stop</button>'
        f'<select class="tiny" data-speed aria-label="Speed">{speeds}</select>'
        '<span class="meta" data-status aria-live="polite"></span></div>'
    )


def reader_page(
    *,
    secret: str,
    csrf: str,
    items: list[dict],
    counts: dict[str, int],
    view: str,
    sources: list[str],
    source: str | None = None,
    issue: str | None = None,
    issue_title: str | None = None,
    selected: dict | None = None,
    body: str | None = None,
    voice: str | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """The mailbox: all the news on the left, the chosen article on the right.

    No JavaScript is required: a click is a navigation that re-renders both
    panes, so the list can carry real forms whose state the server owns. One
    hashed script restores the list's scroll after that navigation. With
    `issue`, the list is that edition instead of the live stream."""
    base = f"/{escape(secret)}"
    tabs = "".join(
        f'<a href="{_reader_href(secret, key, source=source, issue=issue)}"'
        + (' aria-current="page"' if key == view else "")
        + f">{label} ({counts.get(key, 0)})</a>"
        for key, label in _READER_VIEWS
    )
    all_link = (
        f'<a href="{_reader_href(secret, view, issue=issue)}"'
        + (' aria-current="page"' if not source else "")
        + ">All</a>"
    )
    source_links = "".join(
        f'<a href="{_reader_href(secret, view, source=name, issue=issue)}"'
        + (' aria-current="page"' if name == source else "")
        + f">{escape(name)}</a>"
        for name in sources
    )
    if issue:
        filter_form = (
            f'<div class="source-filter"><span class="label">Issue</span>'
            f"<span>{escape(issue_title or issue)}</span>"
            f'<a href="{base}/issues/{escape(issue)}">Issue page</a>'
            f'<a href="{base}/read">All news</a></div>'
        )
    else:
        scope = f" from {escape(source)}" if source else ""
        bulk = "".join(
            _button(
                f"{base}/read/archive-all",
                csrf,
                label,
                style="tiny",
                fields={"keep": keep, "source": source or ""},
            )
            for keep, label in (("liked", "Archive all not liked"), ("none", "Archive all"))
        )
        filter_form = (
            f'<div class="source-filter"><span class="label">Source</span>'
            f"{all_link}{source_links}</div>"
            # Folded away: one stray click should not empty the reader.
            f'<details class="bulk-archive"><summary class="meta">Archive in bulk{scope}…</summary>'
            f'<p class="meta">Queued articles stay. Archived articles stay under the Archived tab.</p>'
            f'<div class="row-actions">{bulk}</div></details>'
        )
    empty = (
        "This issue has no articles here."
        if issue
        else (
            "Nothing from this source here."
            if source
            else f'Nothing here. <a href="{base}/feeds">Add a feed</a> or save a link.'
        )
    )
    rows = "".join(
        _reader_row(
            secret, csrf, item, view, source, issue, selected["id"] if selected else ""
        )
        for item in items
    ) or f'<p class="meta">{empty}</p>'
    if selected and body is not None:
        pane = _reader_pane(
            secret=secret,
            csrf=csrf,
            view=view,
            source=source,
            issue=issue,
            selected=selected,
            body=body,
            voice=voice,
        )
    else:
        pane = (
            '<div class="card reader-pane"><p class="meta">'
            "Pick an article from the list to read it here.</p></div>"
        )
    if selected and body is not None:
        close = _reader_href(secret, view, source=source, issue=issue)
        pane_col = (
            '<div class="reader-pane-col open" role="dialog" aria-modal="true" '
            f'aria-label="{escape(selected["title"])}">'
            f'<div class="reader-close"><a class="btn tiny" href="{close}">✕ Close</a></div>'
            f"{pane}</div>"
        )
    else:
        pane_col = f'<div class="reader-pane-col">{pane}</div>'
    page_title = f"Issue · {issue_title}" if issue else "Reader"
    lede = (
        "The articles printed in this issue. Switch to All news for everything else."
        if issue
        else "Everything your sources publish. Like what is interesting; add what "
        "you want to read next."
    )
    body_html = f"""
<div class="page-head">
  <h1>{escape(page_title)}</h1>
  <p class="meta count" style="margin:.5rem 0 0">{counts.get("unread", 0)} unread ·
  {counts.get("queued", 0)} in the next issue · {counts.get("archived", 0)} archived</p>
</div>
<p class="lede" style="margin-bottom:0">{lede}</p>
<div class="tabs">{tabs}</div>
{filter_form}
<div class="reader-split">
  <div class="card reader-list">{rows}</div>
  {pane_col}
</div>
<script>{READER_SCROLL_JS}</script>
<script>{READER_ACTIONS_JS}</script>
<script>{READER_LISTEN_JS}</script>
"""
    return _shell(
        secret, "read", "Reader", body_html, notice=notice, error=error, wide=True
    )


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
    base = f"/{escape(secret)}"
    meta = []
    if minutes:
        meta.append(f"about {minutes} minute" + ("" if minutes == 1 else "s"))
    if author:
        meta.insert(0, escape(author))
    meta_line = (
        f'<p class="meta" style="margin-bottom:1.4rem">{" · ".join(meta)}</p>'
        if meta
        else ""
    )
    original = (
        f'<a class="btn tiny" href="{base}/read/open?article='
        f'{quote(article_id, safe="")}" target="_blank" '
        'rel="noopener noreferrer">Open original</a>'
        if source_url
        else ""
    )
    save_form = (
        f"""<form method="post" action="{base}/save">
  <input type="hidden" name="back" value="read">
  <input type="hidden" name="url" value="{escape(source_url)}">
  <button class="btn tiny" type="submit">Save for the next issue</button>
</form>"""
        if source_url and not article_id.startswith("saved:")
        else ""
    )
    return _shell(
        secret,
        "read",
        title,
        f"""
<p class="meta"><a href="{base}/read">&larr; Back to the reader</a></p>
<h1>{escape(title)}</h1>
{meta_line}
<div class="article">{body}</div>
<div class="actions">
  {original}
  {save_form}
  {_button(f"{base}/read/{quote(article_id, safe=':-_')}/unread", csrf, "Mark unread", style="tiny")}
  <a class="btn tiny" href="{base}/read">Next unread</a>
</div>
""",
    )


# --------------------------------------------------------- outside a catalogue


def landing_page(
    *,
    payment_link: str | None,
    free_signup: bool,
    base_url: str,
    catalogues: list[str],
) -> str:
    description = (
        "A daily EPUB from your RSS feeds and saved articles, delivered to any "
        "e-reader over OPDS."
    )
    head = (
        '<meta property="og:title" content="Matins: your morning paper, on e-ink">'
        f'<meta property="og:description" content="{description}">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="Matins">'
        f'<meta property="og:url" content="{escape(base_url)}/">'
        '<meta name="twitter:card" content="summary">'
        f'<meta name="description" content="{description}">'
    )
    mine = ""
    if catalogues:
        rows = "".join(
            f"""<div class="row"><div class="row-main"><a class="row-title" href="/{escape(secret)}/">{escape(secret)}</a></div>
<div class="row-actions"><a class="btn tiny primary" href="/{escape(secret)}/">Open</a></div></div>"""
            for secret in catalogues
        )
        mine = f"""<h2 style="margin-top:.5rem">Your catalogues on this device</h2>
<div class="card lead">{rows}</div>"""
    if payment_link:
        start = f'<a class="btn" href="{escape(payment_link)}">Get a catalogue</a>'
    elif free_signup:
        start = '<a class="btn" href="/start">Start without an email</a>'
    else:
        start = ""
    return _public(
        "Matins: your morning paper, on e-ink",
        f"""{mine}
<section class="hero">
  <h1>Your morning paper, on <span style="white-space:nowrap">e-ink</span>.</h1>
  <p>Matins gathers your feeds and saved articles into one tidy EPUB every morning, ready on
  your e-reader before you are.</p>
  <div class="actions">
    <a class="btn primary" href="/signin">Sign in or sign up with email</a>
    {start}
  </div>
</section>

<h2>How it works</h2>
<div class="grid2">
  <div class="card"><h3>1. Choose your sources</h3><p class="small">Paste the sites you follow, import an
  OPML file, or save single articles as you find them.</p></div>
  <div class="card"><h3>2. Review if you like</h3><p class="small">See what the next issue will hold and
  skip what you don't want. Or don't, and let it arrive.</p></div>
  <div class="card"><h3>3. Read offline</h3><p class="small">Open it in KOReader, CrossPoint or any
  OPDS reader, or download the EPUB. No app to install.</p></div>
</div>
<p class="small">Calm by design: fixed limits per issue, nothing printed twice, and no attachment
in your inbox, just a short note when it's ready.</p>

<h2>Self-host it</h2>
<div class="card lead">
  <p class="headline">Don't like me? Clone me.</p>
  <p class="small">Matins is free software (MIT) and meant to be yours: one container,
  one SQLite file, no account to create and no vendor in the middle. Your feeds,
  your queue, your issues and the catalogue address stay on your own machine — a
  Raspberry Pi is enough. Point your e-reader at it and nothing leaves the house.</p>
  <div class="actions">
    <a class="btn primary" href="{REPO_URL}">Clone on GitHub</a>
    <a class="btn" href="{REPO_URL}#self-host-quickstart">Self-host quickstart</a>
  </div>
</div>
""",
        head=head,
    )


def start_page(session_id: str | None, error: str | None = None) -> str:
    hidden = (
        f'<input type="hidden" name="session_id" value="{escape(session_id)}">'
        if session_id
        else ""
    )
    return _public(
        "Create a catalogue",
        f"""<h1>Create a catalogue</h1>
{_flash(None, error)}
<p class="lede">No email needed. You will get a private address made of four words; it is the
only key, so keep it somewhere safe.</p>
<form method="post" action="/start">
{hidden}
<div class="card">
  <label for="readwise_token" style="margin-top:0">Readwise access token (optional)</label>
  <input type="password" id="readwise_token" name="readwise_token" autocomplete="off">
  <p class="hint">Only if you want your Readwise Reader queue in the catalogue too. Get it at
  <a href="https://readwise.io/access_token">readwise.io/access_token</a>. Encrypted at rest,
  never logged, sent only to Readwise.</p>
  <div class="actions"><button class="btn primary" type="submit">Create my catalogue</button></div>
</div>
</form>
<p class="meta"><a href="/signin">Prefer to sign in with an email &rarr;</a></p>
""",
    )


def welcome_page(catalog_url: str, secret: str, csrf: str) -> str:
    base = f"/{escape(secret)}"
    return _public(
        "Your catalogue is ready",
        f"""<h1>Your catalogue is ready</h1>
<p class="lede">This is your private address. Anyone who has it can open your catalogue, so
treat it like a password.</p>
<div class="card lead">
  <span class="url url-block" style="margin:0">{escape(catalog_url)}</span>
  <div class="actions">
    <a class="btn primary" href="{base}/feeds">Add your first feed &rarr;</a>
    {_button(f"{base}/regenerate", csrf, "Pick different words")}
  </div>
</div>
<p class="meta">Bookmark it, or add an email later in Settings to sign in with a one-time code.</p>
""",
    )


def deleted_page() -> str:
    return _public(
        "Deleted",
        """<h1>All gone</h1>
<div class="card"><p>Your catalogue, its feeds, saved links, issues and any stored token have
been deleted.</p><div class="actions"><a class="btn" href="/">Back to Matins</a></div></div>""",
    )


def error_page(status: int, detail: str) -> str:
    titles = {404: "Not found", 410: "Gone", 429: "Slow down", 403: "Not allowed"}
    title = titles.get(status, "Something went wrong")
    hint = (
        "If this was your catalogue, signing in with your email will find it."
        if status == 404
        else "Go back and try again."
    )
    return _public(
        title,
        f"""<h1>{escape(title)}</h1>
<div class="card"><p>{escape(detail)}</p><p class="meta">{hint}</p>
<div class="actions"><a class="btn primary" href="/signin">Sign in</a>
<a class="btn" href="/">Home</a></div></div>""",
    )


def signin_page(*, notice: str | None = None, error: str | None = None) -> str:
    return _public(
        "Sign in",
        f"""<h1>Sign in</h1>
<p class="lede">Enter your email and we will send you a six-digit code. The same address always
opens the same catalogue, on any device.</p>
{_flash(notice, error)}
<form method="post" action="/signin" class="card">
  <label for="email" style="margin-top:0">Email</label>
  <input type="email" id="email" name="email" placeholder="you@example.com" autocomplete="email" required autofocus>
  <div class="actions"><button class="btn primary" type="submit">Send me a code</button></div>
</form>
<p class="meta">First time? The same form creates a catalogue for that address.</p>
<p class="meta"><a href="/start">Prefer no email, or a Readwise token &rarr;</a></p>
""",
    )


def code_page(*, email: str, error: str | None = None) -> str:
    return _public(
        "Check your email",
        f"""<h1>Check your email</h1>
<p class="lede">We sent a six-digit code to <strong>{escape(email)}</strong>. It expires in 10 minutes.</p>
{_flash(None, error)}
<form method="post" action="/signin/code" class="card">
  <input type="hidden" name="email" value="{escape(email)}">
  <label for="code" style="margin-top:0">Code</label>
  <input type="text" id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
    pattern="[0-9]*" maxlength="6" placeholder="123456" required autofocus>
  <div class="actions"><button class="btn primary" type="submit">Sign in</button></div>
</form>
<form method="post" action="/signin">
  <input type="hidden" name="email" value="{escape(email)}">
  <div class="actions"><button class="btn tiny" type="submit">Send another code</button>
  <a class="meta" href="/signin">Use a different address</a></div>
</form>
""",
    )
