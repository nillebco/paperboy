# Matins: architecture and decisions

Matins is the Matins fork's service layer on top of Later.Ink. This note records
*why* the pieces look the way they do, so a future change does not have to
rediscover the constraints.

## The shape

```
feeds (RSS/Atom) ─┐
saved queue      ─┼─► TenantReader ─► IssueService ─► EPUB on disk
Readwise         ─┘   (one catalogue)   selection      │
                                                       ├─► OPDS  /<secret>/issues/
                                                       ├─► webhook  issue.ready
                                                       └─► email    (link only)
```

Everything user-facing hangs off the catalogue secret (`/<secret>/`), the same
unguessable-in-practice bearer URL Later.Ink already uses. There is no login,
because an e-reader cannot do one conveniently — the URL *is* the credential.

## Why a fork rather than a plugin

Later.Ink's connectors are one-account-per-connector, and a tenant maps to one
connector. Issues need the union of several sources per user, plus write access
to a queue, plus scheduled work. All three are outside the connector contract,
so the fork adds:

| Piece | File |
|---|---|
| Multi-source fan-out with id namespacing | `matins/tenant.py` |
| RSS/Atom parsing and fetching | `matins/rss.py` |
| Built-in queue | `matins/queue.py` |
| Article extraction | `matins/extract.py` |
| Issue composition | `matins/issue_epub.py`, `matins/issues.py` |
| Signed webhooks | `matins/webhooks.py` |
| Notification email | `matins/notify.py` |
| Scheduler (asyncio loop) | `matins/scheduler.py` |
| The app: home, feeds, next issue, issues, settings; admin | `matins/routes.py`, `matins/pages.py` |
| Tables (feeds, saved, issues, delivery) | `matins/db.py` |

`later_ink/main.py` changes in four places: the router include, the tenant
connector factory, the Issues entry in the catalogue, and the lifespan wiring.
Keeping the diff there small is what makes `git merge upstream/main` survivable.

## Decisions worth remembering

**No new runtime dependencies.** RSS, extraction, HMAC, scheduling and SMTP are
`lxml` + stdlib. Every added package is a supply-chain surface to pin, hash and
patch; upstream already goes to real lengths here (hashed lockfiles,
digest-pinned images, locked build backend, `pip-audit` in CI). Extraction is the
weak point of this trade — the heuristic in `extract.py` is a simplified
readability — but it degrades to the feed's own content rather than failing.

**Ids: prefixed for new sources, bare for Readwise.** `feeds:…` and `saved:…`
route a download back to their connector; Readwise keeps upstream's bare ids so
existing OPDS URLs and KOReader progress entries survive. Unprefixed ids resolve
to Readwise, which is also what an old client sends.

**Dedupe keys are catalogue ids, not connector ids.** An issue records
`issue_items` with the same string the download route uses. The first
implementation compared raw connector ids against stored prefixed ones and
reprinted the whole feed every day; the test
`test_compose_now_builds_a_downloadable_issue` covers the regression.

**Email is notification-only.** No attachment, no Send-to-Kindle path, no 25 MB
ceiling. It links to the issue's browser page (contents + EPUB download) first,
then to the OPDS catalogue; the issue itself is pulled. The `issue.ready`
webhook carries the same `issue_url` (and `issue.web_url`). This also means SMTP being down never costs a
reader their issue.

**Webhooks are SSRF-guarded.** The URL is user-supplied; the same address check
the image fetcher applies (`fetch._validate`, public IPs only) runs before every
attempt, and redirects are not followed. `WEBHOOK_ALLOW_PRIVATE=1` is the
self-hoster's explicit opt-out for LAN targets such as Home Assistant.

**An issue is written to disk, not rendered per request.** It is an edition:
fixed membership, fixed bytes. That is what lets two devices share reading
position through KOReader's content-hash matching.

**Catch-up, not a missed day.** The scheduler considers a job due any time after
its scheduled local time while today's issue is missing, so a restart at 09:00
still produces the 07:00 paper. Failed builds retry every 30 minutes rather than
every tick.

**One shell, no script.** Every catalogue page goes through `pages._shell`, so
navigation and post-action messages live in one place. Confirmations are
`<details>`, and a slow compose hands over to a page with a meta refresh: the CSP
stays script-free and the app works on the e-ink browsers it serves.

**The first feed turns on the daily issue.** The scheduler only sees catalogues
with a `delivery` row. Adding a feed or signing in by email creates one; an
explicit "off" is left alone.

**Feed health is recorded, not fetched per page.** Each real fetch writes its
outcome to `feeds`, so the feeds page needs no network. A failed fetch is not
retried for five minutes unless the user asks.

**A rotated URL moves the catalogue.** `MatinsStore.rename_secret` covers every
table in `_SECRET_TABLES`; a new secret-keyed table must be added there.

**Reading time is shown only when known.** A feed's summary says nothing about the
article's length, so previews show a time only for full-text items. The word
budget still uses those estimates, so an issue can run past `max_words`.

## Data

SQLite, same file as the users table (WAL), disjoint tables:

- `feeds (id, secret, url, title, added_at, paused, last_fetched_at, last_error,
  item_count, last_item_at, auth_cookie)` — unique per `(secret, url)`.
  `auth_cookie` is an optional subscriber session cookie, Fernet-encrypted with
  the app key, used only when fetching that feed's article pages (never for the
  RSS fetch, and dropped after a redirect to another site).
- `saved (id, secret, url, title, added_at, consumed_by)` — the queue; re-saving
  a consumed link re-queues it.
- `issues (id, secret, title, created_at, local_day, path, article_count,
  word_count)` — `local_day` is the user's calendar day, used to answer "already
  built today?".
- `issue_items (issue_id, secret, article_id, title, url, section)` — membership
  and the dedupe set.
- `delivery (secret, webhook_url, webhook_secret, email, schedule, timezone,
  enabled, auto_select, max_articles, max_per_feed, max_words, window_hours)` —
  the webhook secret is Fernet-encrypted with the app key; a NULL limit means the
  instance default. `auto_select=0` is the curated mode: an issue is the queue
  alone, and feeds are for browsing.
- `candidates (id, secret, url, title, source, summary, content, published)` —
  browsable items the composer must not print (Bluesky). Queuing one copies its
  `content`, because the URL cannot be fetched.
- `article_signals (secret, url, title, source, rating)` — likes (+1) and
  not-for-me (−1), keyed by URL so a curator can consume them.
- `issue_skips (secret, article_id, title, section, url, skipped_at)` — articles
  taken out of the next issue.

Issue files: `ISSUES_DIR/<sha1(secret)[:16]>/<issue-id>.epub`, written to a temp
name then renamed. The download route looks the path up in the database and
never builds one from the request.

## Configuration

See `deploy/oci/env.example` for the annotated list. The ones that change
behaviour meaningfully: `ISSUE_WINDOW_HOURS` (how far back a feed item may be),
`ISSUE_MAX_ARTICLES` / `ISSUE_MAX_PER_FEED` / `ISSUE_MAX_WORDS`,
`ISSUE_RETENTION`, `ADMIN_TOKEN`, and the SMTP group.

## API

`/api/v1/*`, authenticated with the catalogue secret as a bearer token — not in
the URL, so it stays out of access logs. Designed for an agent or a script; the
pages use the same service layer, so nothing here has powers the UI lacks.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/items?status=unread\|read\|all` | the saved queue and its read state |
| POST | `/api/v1/items` | queue `{url, title?, content?}` |
| DELETE | `/api/v1/items/{id}` | drop a queued link |
| GET/POST | `/api/v1/feeds` | list / subscribe `{url}` |
| DELETE | `/api/v1/feeds/{id}` | unsubscribe |
| GET | `/api/v1/issues` | composed issues and download paths |
| GET | `/api/v1/issues/preview` | exactly what the next issue would contain (`only=queue` for a curated preview) |
| POST | `/api/v1/issues?force=0\|1` | compose and deliver now (409 if today's exists); `only=queue` composes the queue alone |
| GET/POST | `/api/v1/signals` | likes, not-for-me, reads and opens, keyed by URL with a weight |
| GET/POST | `/api/v1/candidates` | browsable-but-not-printed items (Bluesky posts) |

```bash
S=your-four-word-secret
curl -sS -X POST https://matins.example/api/v1/items \
  -H "Authorization: Bearer $S" -H 'content-type: application/json' \
  -d '{"url":"https://example.com/post","title":"From an agent"}'
```

`content` is the interesting field for agents: when supplied, the body is used
as-is instead of the server fetching the page (capped at 512 KiB). That is how
you queue something the server cannot reach — a paywall, an authenticated page,
a generated summary — and it still goes through the same sanitising EPUB path.

### Paywalled publishers

A feed's detail page has a **Subscriber access** field: paste the `Cookie`
request header from a logged-in browser tab, and Matins will use it when fetching
that feed's article pages. The value is encrypted at rest, never rendered back,
and only sent to hosts on the same registrable site as the article (a redirect
to another domain drops it). The feed's RSS is always fetched without it.

This is a session, not a licence: when the publisher logs the browser out, the
cookie expires and the reader falls back to the feed's lede until a new one is
pasted. Some publishers also challenge non-browser clients independently of the
subscription (Le Monde's JS wall); a cookie cannot defeat that.

`GET /api/v1/signals` returns `{url, title, source, rating, weight, kind}`. An
explicit like/dislike is `kind: like|dislike`, `weight: ±1`, and wins over
implicit events. Otherwise opening an article in the reader is `kind: read`,
`weight: 0.35`, and handing it to the publisher (`/read/open`) is
`kind: opened`, `weight: 0.7`. A curator should aggregate `weight`, not count
rows: ten reads are not ten likes.

`only=queue` is the curator's option. A client that already chose the day's
articles queues them and composes with `POST /api/v1/issues?only=queue`: the
automatic feed selection is skipped, so the edition contains exactly the queue
(and the queue's own cap). Preview accepts the same parameter, so the choice can
be checked before anything is written. Both endpoints reject any other `only`
value; "everything" is the absence of the parameter.

Why not Readwise: it works only if the catalogue has a Readwise token wired in,
it costs a subscription, and it cannot express feeds. Matins owns the queue and
the feeds, so its own API is the honest integration point.

## Known limits

- No full-text extraction from paywalled or JS-only pages; those fall back to
  the feed summary.
- A feed that dates nothing is always considered fresh (it would otherwise never
  be printed), so a static page can reappear if its link changes.
- Search across sources is capped, not paginated.
- Issue language is fixed to `en` until per-feed language is tracked.
- No metrics endpoint; `/health` reports configuration, logs report builds.
