"""Matins storage: feeds, the built-in read-later queue, issues, delivery.

Deliberately a separate class from `later_ink.store` even though both open the
same SQLite file: upstream owns the users/rate-limit schema and changes it
often, and a fork that edits those tables turns every upstream commit into a
merge conflict. Same file, same WAL, disjoint tables.

Nothing here is account content by default — feeds list URLs the user chose,
and the read-later queue stores links, not article bodies. The one secret kept
is the webhook signing key, Fernet-encrypted with the app key so a stolen copy
of app.db cannot be used to forge deliveries to a user's endpoint.
"""

import hashlib
import os
import secrets as pysecrets
import sqlite3
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class Feed:
    id: str
    url: str
    title: str
    # A paused feed stays subscribed and readable but stops feeding issues.
    paused: bool = False
    # Outcome of the last real fetch, so the feeds page can say which sources
    # are healthy without hitting every publisher on each page load.
    last_fetched_at: float | None = None
    last_error: str | None = None
    item_count: int | None = None
    last_item_at: float | None = None
    # Subscriber session cookie for paywalled publishers (Le Monde). Encrypted
    # at rest and never rendered back into a page.
    auth_cookie: str | None = None


@dataclass(frozen=True)
class SavedItem:
    id: int
    url: str
    title: str | None
    added_at: float
    # Clean article HTML supplied by whoever saved the link (an agent, a share
    # extension with a reader mode). When present it is used instead of fetching
    # the page, which is the only way to queue something unfetchable.
    content: str | None = None
    consumed_by: str | None = None
    # "user" for anything a person picked, "curator" for an agent's picks. User
    # picks print first; a curator's picks only fill what is left.
    origin: str = "user"
    # The feed's own summary: what prints when the page itself cannot be fetched.
    lede: str | None = None
    # Short summary printed above a very long article.
    recap: str | None = None


@dataclass(frozen=True)
class Issue:
    id: str
    secret: str
    title: str
    created_at: float
    path: str
    article_count: int
    word_count: int


@dataclass(frozen=True)
class Delivery:
    secret: str
    webhook_url: str | None
    webhook_secret: str | None
    email: str | None
    schedule: str
    timezone: str
    enabled: bool
    # Per-catalogue overrides of the instance's issue limits. None = the default.
    max_articles: int | None = None
    max_per_feed: int | None = None
    max_words: int | None = None
    max_saved: int | None = None
    window_hours: int | None = None
    # Automatic: compose from feeds + queue. Curated: print only the queue.
    auto_select: bool = True


@dataclass(frozen=True)
class IssueEntry:
    article_id: str
    title: str
    url: str | None
    section: str


@dataclass(frozen=True)
class SkippedArticle:
    article_id: str
    title: str
    section: str
    url: str | None


@dataclass(frozen=True)
class CandidateItem:
    """An item an external curator offers for browsing but not for printing.

    Bluesky posts are the first use: they cannot be fetched by URL, so their text
    arrives with them. They are visible in the reader and enter an issue only
    when the user (or the curator) queues them."""

    id: str
    url: str
    title: str
    source: str
    summary: str
    content: str | None
    published: float | None
    added_at: float


@dataclass(frozen=True)
class ArchivedArticle:
    """A discarded item. Kept by URL so a later fetch does not resurrect it."""

    id: str
    url: str
    article_id: str | None
    title: str
    source: str
    archived_at: float
    # Original source kind ("candidate", "rss", "later"), so the source filter
    # can still tell a discarded Bluesky post from a discarded feed article.
    kind: str | None = None


@dataclass(frozen=True)
class ArticleEvent:
    """An implicit interest signal: the article was opened, or read in-app."""

    url: str
    article_id: str | None
    title: str
    source: str
    kind: str  # "read" | "opened"
    created_at: float


@dataclass(frozen=True)
class ArticleSignal:
    """One like/dislike, keyed by URL so an external curator can use it too."""

    url: str
    article_id: str | None
    title: str
    source: str
    rating: int
    updated_at: float


def feed_id(secret: str, url: str) -> str:
    """Stable short id for one subscription. Short because it travels in OPDS
    folder URLs that get typed on an e-ink keyboard."""
    return hashlib.sha1(f"{secret}\x00{url}".encode()).hexdigest()[:12]


class MatinsStore:
    def __init__(self, path: str, fernet: Fernet):
        self.path = path
        self._fernet = fernet
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feeds (
                    id TEXT PRIMARY KEY,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    added_at REAL NOT NULL,
                    UNIQUE (secret, url)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS feeds_by_user ON feeds (secret)")
            _ensure_column(conn, "feeds", "paused", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "feeds", "last_fetched_at", "REAL")
            _ensure_column(conn, "feeds", "last_error", "TEXT")
            _ensure_column(conn, "feeds", "item_count", "INTEGER")
            _ensure_column(conn, "feeds", "last_item_at", "REAL")
            _ensure_column(conn, "feeds", "auth_cookie", "TEXT")
            # The queue a user fills with the share sheet or POST /{secret}/save.
            # consumed_by records which issue took the item, so a link is never
            # printed twice even across restarts.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saved (
                    id INTEGER PRIMARY KEY,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    added_at REAL NOT NULL,
                    consumed_by TEXT,
                    UNIQUE (secret, url)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS saved_pending ON saved (secret, consumed_by)"
            )
            _ensure_column(conn, "saved", "content", "TEXT")
            _ensure_column(conn, "saved", "origin", "TEXT NOT NULL DEFAULT 'user'")
            _ensure_column(conn, "saved", "lede", "TEXT")
            _ensure_column(conn, "saved", "recap", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    secret TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    local_day TEXT NOT NULL,
                    path TEXT NOT NULL,
                    article_count INTEGER NOT NULL,
                    word_count INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS issues_by_user ON issues (secret, created_at)"
            )
            # What went into each issue. Doubles as the dedupe set for feed
            # articles, whose upstream ids are derived from the link and so stay
            # stable between fetches.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_items (
                    issue_id TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    section TEXT NOT NULL,
                    PRIMARY KEY (issue_id, article_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS issue_items_seen"
                " ON issue_items (secret, article_id)"
            )
            # Email -> catalogue, so a one-time code can recover a catalogue on
            # a new device. One address maps to one catalogue: an email that
            # opened two would have to guess which one a code meant.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    email TEXT PRIMARY KEY,
                    secret TEXT NOT NULL,
                    linked_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS emails_by_secret ON emails (secret)"
            )
            # One live challenge per address. Only a hash is stored: a database
            # copy must not be enough to replay a code that is still valid.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS otp (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at REAL NOT NULL
                )
                """
            )
            # Read state for the in-browser reader. Separate from issues: having
            # printed an article is not the same as having read it, and one must
            # not imply the other in the unread count.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reads (
                    secret TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    read_at REAL NOT NULL,
                    PRIMARY KEY (secret, article_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery (
                    secret TEXT PRIMARY KEY,
                    webhook_url TEXT,
                    webhook_secret TEXT,
                    email TEXT,
                    schedule TEXT NOT NULL DEFAULT '07:00',
                    timezone TEXT NOT NULL DEFAULT 'Europe/Paris',
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            for column in ("max_articles", "max_per_feed", "max_words", "window_hours", "max_saved"):
                _ensure_column(conn, "delivery", column, "INTEGER")
            _ensure_column(conn, "delivery", "auto_select", "INTEGER NOT NULL DEFAULT 1")
            # Articles the user took out of the next issue. Kept with their title
            # so the list can offer "put it back" without refetching the feed.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_skips (
                    secret TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    section TEXT NOT NULL,
                    url TEXT,
                    skipped_at REAL NOT NULL,
                    PRIMARY KEY (secret, article_id)
                )
                """
            )
            # Browsable but not automatically printable. A curator pushes them
            # (Bluesky today); the reader lists them next to the feeds.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    content TEXT,
                    published REAL,
                    added_at REAL NOT NULL,
                    PRIMARY KEY (secret, id),
                    UNIQUE (secret, url)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS candidates_by_user"
                " ON candidates (secret, added_at)"
            )
            # Discarded from the reader. A separate table from signals because
            # "do not show me this" is not the same statement as "not for me" —
            # though archiving also records the latter for the curator.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS archived_articles (
                    id TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    article_id TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    archived_at REAL NOT NULL,
                    PRIMARY KEY (secret, url)
                )
                """
            )
            _ensure_column(conn, "archived_articles", "kind", "TEXT")
            # Implicit interest: opened in the reader, or handed to the
            # publisher. Weaker than a like, but still a statement.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS article_events (
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    article_id TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (secret, url, kind)
                )
                """
            )
            # Like / not for me. Keyed by URL, not by connector id: the curator
            # ranks next morning's feeds and has no catalogue ids to work with.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS article_signals (
                    secret TEXT NOT NULL,
                    url TEXT NOT NULL,
                    article_id TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    rating INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (secret, url)
                )
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ---------------------------------------------------------------- feeds

    def add_feed(self, secret: str, url: str, title: str = "") -> Feed:
        fid = feed_id(secret, url)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO feeds (id, secret, url, title, added_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (secret, url) DO UPDATE SET title = excluded.title",
                (fid, secret, url, title, time.time()),
            )
        return Feed(id=fid, url=url, title=title)

    def list_feeds(self, secret: str) -> list[Feed]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM feeds WHERE secret = ? ORDER BY added_at",
                (secret,),
            ).fetchall()
        return [_feed(r, self._fernet) for r in rows]

    def get_feed(self, secret: str, fid: str) -> Feed | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM feeds WHERE secret = ? AND id = ?",
                (secret, fid),
            ).fetchone()
        return _feed(row, self._fernet) if row else None

    def set_feed_auth(self, secret: str, fid: str, cookie: str | None) -> bool:
        """Store (or clear) the subscriber cookie used to fetch full articles."""
        blob = self._fernet.encrypt(cookie.encode()).decode() if cookie else None
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE feeds SET auth_cookie = ? WHERE secret = ? AND id = ?",
                (blob, secret, fid),
            )
        return cur.rowcount > 0

    def rename_feed(self, secret: str, fid: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE feeds SET title = ? WHERE secret = ? AND id = ?",
                (title, secret, fid),
            )

    def set_feed_paused(self, secret: str, fid: str, paused: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE feeds SET paused = ? WHERE secret = ? AND id = ?",
                (int(paused), secret, fid),
            )
        return cur.rowcount > 0

    def record_feed_fetch(
        self,
        secret: str,
        fid: str,
        *,
        error: str | None = None,
        item_count: int | None = None,
        last_item_at: float | None = None,
    ) -> None:
        """Remember how the last fetch went. A failure keeps the previous counts:
        "this feed had 20 items yesterday and is down now" is the useful answer."""
        now = time.time()
        with self._conn() as conn:
            if error:
                conn.execute(
                    "UPDATE feeds SET last_fetched_at = ?, last_error = ?"
                    " WHERE secret = ? AND id = ?",
                    (now, error, secret, fid),
                )
            else:
                conn.execute(
                    "UPDATE feeds SET last_fetched_at = ?, last_error = NULL,"
                    " item_count = ?, last_item_at = ? WHERE secret = ? AND id = ?",
                    (now, item_count, last_item_at, secret, fid),
                )

    def delete_feed(self, secret: str, fid: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM feeds WHERE secret = ? AND id = ?", (secret, fid)
            )
        return cur.rowcount > 0

    # -------------------------------------------------------- saved queue

    def save_url(
        self,
        secret: str,
        url: str,
        title: str | None = None,
        content: str | None = None,
        *,
        origin: str = "user",
        lede: str | None = None,
    ) -> bool:
        """Queue a link. True if it was new, False if already queued.

        Re-saving a consumed link is allowed and re-queues it: the user's intent
        is "put this back in", and silently ignoring that looks broken. A user
        picking something the curator queued makes it theirs; the reverse never
        demotes a user's pick."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, consumed_by FROM saved WHERE secret = ? AND url = ?",
                (secret, url),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO saved (secret, url, title, added_at, content, origin, lede)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (secret, url, title, time.time(), content, origin, lede),
                )
                return True
            if row["consumed_by"] is not None:
                conn.execute(
                    "UPDATE saved SET consumed_by = NULL, added_at = ?, content = ?,"
                    " origin = ?, lede = COALESCE(?, lede), recap = NULL WHERE id = ?",
                    (time.time(), content, origin, lede, row["id"]),
                )
                return True
            if origin == "user":
                conn.execute("UPDATE saved SET origin = 'user' WHERE id = ?", (row["id"],))
            if lede is not None:
                conn.execute("UPDATE saved SET lede = ? WHERE id = ?", (lede, row["id"]))
            if content is not None:
                # Re-saving a queued link with body text upgrades it: the agent
                # may retry with more context than the first attempt had.
                conn.execute("UPDATE saved SET content = ? WHERE id = ?", (content, row["id"]))
                return True
            return False

    def set_saved_recap(self, secret: str, saved_id: int, recap: str | None) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE saved SET recap = ? WHERE secret = ? AND id = ?",
                (recap, secret, saved_id),
            )
            return cur.rowcount > 0

    def list_saved(self, secret: str, include_consumed: bool = False) -> list[SavedItem]:
        sql = (
            "SELECT id, url, title, added_at, content, consumed_by, origin, lede, recap"
            " FROM saved WHERE secret = ?"
        )
        if not include_consumed:
            sql += " AND consumed_by IS NULL"
        sql += " ORDER BY added_at"
        with self._conn() as conn:
            rows = conn.execute(sql, (secret,)).fetchall()
        return [
            SavedItem(
                id=r["id"],
                url=r["url"],
                title=r["title"],
                added_at=r["added_at"],
                content=r["content"],
                consumed_by=r["consumed_by"],
                origin=r["origin"],
                lede=r["lede"],
                recap=r["recap"],
            )
            for r in rows
        ]

    def get_saved(self, secret: str, saved_id: int) -> SavedItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, url, title, added_at, content, origin, lede, recap FROM saved"
                " WHERE secret = ? AND id = ?",
                (secret, saved_id),
            ).fetchone()
        if row is None:
            return None
        return SavedItem(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            added_at=row["added_at"],
            content=row["content"],
            origin=row["origin"],
            lede=row["lede"],
            recap=row["recap"],
        )

    # --------------------------------------------------------------- email

    def link_email(self, secret: str, email: str) -> None:
        """Attach an address to a catalogue.

        Raises ValueError when the address already belongs to a different
        catalogue: silently stealing it would hand one person's catalogue to
        whoever controls the other mailbox.
        """
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT secret FROM emails WHERE email = ?", (email,)
            ).fetchone()
            if existing is not None and existing["secret"] != secret:
                raise ValueError("that email already opens another catalogue")
            conn.execute("DELETE FROM emails WHERE secret = ?", (secret,))
            conn.execute(
                "INSERT INTO emails (email, secret, linked_at) VALUES (?, ?, ?)"
                " ON CONFLICT (email) DO UPDATE SET secret = excluded.secret,"
                " linked_at = excluded.linked_at",
                (email, secret, time.time()),
            )

    def secret_for_email(self, email: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT secret FROM emails WHERE email = ?", (email,)
            ).fetchone()
        return row["secret"] if row else None

    def email_for_secret(self, secret: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT email FROM emails WHERE secret = ?", (secret,)
            ).fetchone()
        return row["email"] if row else None

    def unlink_email(self, secret: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM emails WHERE secret = ?", (secret,))

    def set_otp(self, email: str, code_hash: str, *, ttl: float) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO otp (email, code_hash, expires_at, attempts, sent_at)"
                " VALUES (?, ?, ?, 0, ?)"
                " ON CONFLICT (email) DO UPDATE SET code_hash = excluded.code_hash,"
                " expires_at = excluded.expires_at, attempts = 0, sent_at = excluded.sent_at",
                (email, code_hash, now + ttl, now),
            )

    def otp_state(self, email: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM otp WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def bump_otp_attempts(self, email: str) -> int:
        with self._conn() as conn:
            conn.execute("UPDATE otp SET attempts = attempts + 1 WHERE email = ?", (email,))
            row = conn.execute("SELECT attempts FROM otp WHERE email = ?", (email,)).fetchone()
        return row["attempts"] if row else 0

    def clear_otp(self, email: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM otp WHERE email = ?", (email,))

    def otp_cooldown_ok(self, email: str, *, cooldown: float) -> bool:
        """One send per address per cooldown. The hourly cap is a durable
        limiter keyed by the address (see routes), not a row count — the row is
        overwritten in place, so counting it would always say "one"."""
        with self._conn() as conn:
            row = conn.execute("SELECT sent_at FROM otp WHERE email = ?", (email,)).fetchone()
        return row is None or time.time() - row["sent_at"] >= cooldown

    # ------------------------------------------------------------- reading

    def mark_read(self, secret: str, article_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reads (secret, article_id, read_at) VALUES (?, ?, ?)"
                " ON CONFLICT (secret, article_id) DO UPDATE SET read_at = excluded.read_at",
                (secret, article_id, time.time()),
            )

    def mark_unread(self, secret: str, article_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM reads WHERE secret = ? AND article_id = ?",
                (secret, article_id),
            )

    def is_read(self, secret: str, article_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM reads WHERE secret = ? AND article_id = ?",
                (secret, article_id),
            ).fetchone()
        return row is not None

    def read_ids(self, secret: str, article_ids: list[str]) -> set[str]:
        seen: set[str] = set()
        for i in range(0, len(article_ids), 500):
            chunk = article_ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT article_id FROM reads WHERE secret = ?"
                    f" AND article_id IN ({placeholders})",
                    (secret, *chunk),
                ).fetchall()
            seen.update(r["article_id"] for r in rows)
        return seen

    def forget_reads(self, secret: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM reads WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM emails WHERE secret = ?", (secret,))

    def delete_saved(self, secret: str, saved_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM saved WHERE secret = ? AND id = ?", (secret, saved_id)
            )
        return cur.rowcount > 0

    def consume_saved(self, secret: str, ids: list[int], issue_id: str) -> None:
        if not ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE saved SET consumed_by = ? WHERE secret = ? AND id = ?",
                [(issue_id, secret, i) for i in ids],
            )

    # -------------------------------------------------------- candidates

    def upsert_candidates(self, secret: str, items: list[dict]) -> int:
        """Store items a curator wants visible in the reader.

        Re-pushing a post updates its text (an edit or a re-fetch) without
        duplicating it. The table is pruned to the newest 500 and 14 days so a
        curator that runs daily cannot grow it without bound.
        """
        now = time.time()
        with self._conn() as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO candidates"
                    " (id, secret, url, title, source, summary, content, published, added_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (secret, url) DO UPDATE SET"
                    " id = excluded.id, title = excluded.title, source = excluded.source,"
                    " summary = excluded.summary, content = excluded.content,"
                    " published = excluded.published, added_at = excluded.added_at",
                    (
                        str(item["id"]),
                        secret,
                        str(item["url"]),
                        str(item.get("title") or ""),
                        str(item.get("source") or ""),
                        str(item.get("summary") or ""),
                        item.get("content"),
                        item.get("published"),
                        now,
                    ),
                )
            conn.execute(
                "DELETE FROM candidates WHERE secret = ? AND added_at < ?",
                (secret, now - 14 * 24 * 3600),
            )
            conn.execute(
                "DELETE FROM candidates WHERE secret = ? AND id NOT IN"
                " (SELECT id FROM candidates WHERE secret = ?"
                "  ORDER BY added_at DESC LIMIT 500)",
                (secret, secret),
            )
        return len(items)

    def list_candidates(self, secret: str, limit: int = 200) -> list[CandidateItem]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE secret = ?"
                " ORDER BY COALESCE(published, added_at) DESC LIMIT ?",
                (secret, max(1, min(limit, 500))),
            ).fetchall()
        return [_candidate(row) for row in rows]

    def candidate_stats(self, secret: str) -> dict[str, int | float | None]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS posts, COUNT(DISTINCT source) AS accounts,"
                " MAX(added_at) AS last_at FROM candidates WHERE secret = ?",
                (secret,),
            ).fetchone()
        return {
            "posts": int(row["posts"] or 0),
            "accounts": int(row["accounts"] or 0),
            "last_at": row["last_at"],
        }

    def get_candidate(self, secret: str, cid: str) -> CandidateItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM candidates WHERE secret = ? AND id = ?", (secret, cid)
            ).fetchone()
        return _candidate(row) if row else None

    # ---------------------------------------------------------- archived

    def archive_article(
        self,
        secret: str,
        url: str,
        *,
        article_id: str | None = None,
        title: str = "",
        source: str = "",
        kind: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO archived_articles"
                " (id, secret, url, article_id, title, source, kind, archived_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (secret, url) DO UPDATE SET"
                " article_id = COALESCE(excluded.article_id, archived_articles.article_id),"
                " title = excluded.title, source = excluded.source,"
                " kind = COALESCE(excluded.kind, archived_articles.kind),"
                " archived_at = excluded.archived_at",
                (
                    _archive_id(url),
                    secret,
                    url,
                    article_id,
                    title,
                    source,
                    kind,
                    time.time(),
                ),
            )

    def unarchive_article(self, secret: str, url: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM archived_articles WHERE secret = ? AND url = ?",
                (secret, url),
            )
        return cur.rowcount > 0

    def get_archived(self, secret: str, archive_id: str) -> ArchivedArticle | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM archived_articles WHERE secret = ? AND id = ?",
                (secret, archive_id),
            ).fetchone()
        return _archived(row) if row else None

    def list_archived(self, secret: str, limit: int = 400) -> list[ArchivedArticle]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM archived_articles WHERE secret = ?"
                " ORDER BY archived_at DESC LIMIT ?",
                (secret, max(1, min(limit, 1000))),
            ).fetchall()
        return [_archived(row) for row in rows]

    def archived_urls(self, secret: str, urls: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(urls), 500):
            chunk = urls[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT url FROM archived_articles WHERE secret = ?"
                    f" AND url IN ({placeholders})",
                    (secret, *chunk),
                ).fetchall()
            found.update(r["url"] for r in rows)
        return found

    # ------------------------------------------------------------ events

    def record_event(
        self,
        secret: str,
        url: str,
        *,
        article_id: str | None = None,
        title: str = "",
        source: str = "",
        kind: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO article_events"
                " (secret, url, article_id, title, source, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (secret, url, kind) DO UPDATE SET"
                " article_id = COALESCE(excluded.article_id, article_events.article_id),"
                " title = excluded.title, source = excluded.source,"
                " created_at = excluded.created_at",
                (secret, url, article_id, title, source, kind, time.time()),
            )

    def list_events(self, secret: str, limit: int = 1000) -> list[ArticleEvent]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM article_events WHERE secret = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (secret, max(1, min(limit, 3000))),
            ).fetchall()
        return [
            ArticleEvent(
                url=r["url"],
                article_id=r["article_id"],
                title=r["title"],
                source=r["source"],
                kind=r["kind"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------- signals

    def set_signal(
        self,
        secret: str,
        url: str,
        rating: int,
        *,
        article_id: str | None = None,
        title: str = "",
        source: str = "",
    ) -> None:
        if rating == 0:
            self.clear_signal(secret, url)
            return
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO article_signals"
                " (secret, url, article_id, title, source, rating, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (secret, url) DO UPDATE SET"
                " article_id = COALESCE(excluded.article_id, article_signals.article_id),"
                " title = excluded.title, source = excluded.source,"
                " rating = excluded.rating, updated_at = excluded.updated_at",
                (
                    secret,
                    url,
                    article_id,
                    title,
                    source,
                    1 if rating > 0 else -1,
                    now,
                    now,
                ),
            )

    def clear_signal(self, secret: str, url: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM article_signals WHERE secret = ? AND url = ?",
                (secret, url),
            )

    def signals_for(self, secret: str, urls: list[str]) -> dict[str, int]:
        ratings: dict[str, int] = {}
        for i in range(0, len(urls), 500):
            chunk = urls[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT url, rating FROM article_signals WHERE secret = ?"
                    f" AND url IN ({placeholders})",
                    (secret, *chunk),
                ).fetchall()
            ratings.update({r["url"]: r["rating"] for r in rows})
        return ratings

    def list_signals(self, secret: str, limit: int = 500) -> list[ArticleSignal]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM article_signals WHERE secret = ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (secret, max(1, min(limit, 2000))),
            ).fetchall()
        return [
            ArticleSignal(
                url=r["url"],
                article_id=r["article_id"],
                title=r["title"],
                source=r["source"],
                rating=r["rating"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # -------------------------------------------------------------- issues

    def new_issue_id(self) -> str:
        return f"{time.strftime('%Y%m%d')}-{pysecrets.token_hex(3)}"

    def record_issue(
        self,
        *,
        issue_id: str,
        secret: str,
        title: str,
        local_day: str,
        path: str,
        article_count: int,
        word_count: int,
        items: list[tuple[str, str, str | None, str]],
    ) -> Issue:
        created = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO issues (id, secret, title, created_at, local_day, path,"
                " article_count, word_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    secret,
                    title,
                    created,
                    local_day,
                    path,
                    article_count,
                    word_count,
                ),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO issue_items"
                " (issue_id, secret, article_id, title, url, section)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(issue_id, secret, aid, at, au, sec) for aid, at, au, sec in items],
            )
        return Issue(
            id=issue_id,
            secret=secret,
            title=title,
            created_at=created,
            path=path,
            article_count=article_count,
            word_count=word_count,
        )

    def find_issue_item(self, secret: str, article_id: str) -> IssueEntry | None:
        """The last issue that printed an article, for the reader's issue view."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT article_id, title, url, section FROM issue_items"
                " WHERE secret = ? AND article_id = ? LIMIT 1",
                (secret, article_id),
            ).fetchone()
        if row is None:
            return None
        return IssueEntry(
            article_id=row["article_id"],
            title=row["title"],
            url=row["url"],
            section=row["section"],
        )

    def issue_entries(self, secret: str, issue_id: str) -> list[IssueEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT article_id, title, url, section FROM issue_items"
                " WHERE secret = ? AND issue_id = ? ORDER BY rowid",
                (secret, issue_id),
            ).fetchall()
        return [
            IssueEntry(
                article_id=r["article_id"], title=r["title"], url=r["url"], section=r["section"]
            )
            for r in rows
        ]

    def release_issue(self, secret: str, issue_id: str) -> None:
        """Forget an issue and hand its articles back to the next one."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE saved SET consumed_by = NULL WHERE secret = ? AND consumed_by = ?",
                (secret, issue_id),
            )
        self.delete_issue(secret, issue_id)

    def set_issue_path(self, secret: str, issue_id: str, path: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE issues SET path = ? WHERE secret = ? AND id = ?",
                (path, secret, issue_id),
            )

    def list_issues(self, secret: str, limit: int = 50) -> list[Issue]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM issues WHERE secret = ? ORDER BY created_at DESC LIMIT ?",
                (secret, limit),
            ).fetchall()
        return [_issue(r) for r in rows]

    def get_issue(self, secret: str, issue_id: str) -> Issue | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM issues WHERE secret = ? AND id = ?", (secret, issue_id)
            ).fetchone()
        return _issue(row) if row else None

    def delete_issue(self, secret: str, issue_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM issue_items WHERE issue_id = ? AND secret = ?",
                (issue_id, secret),
            )
            conn.execute(
                "DELETE FROM issues WHERE id = ? AND secret = ?", (issue_id, secret)
            )

    def delete_user_data(self, secret: str) -> None:
        """Remove everything Matins stores for a catalogue.

        Deleting a catalogue has to mean deleting the feeds someone follows,
        the links they saved, and the issues already printed — otherwise "delete
        my catalogue" leaves a readable record of their reading behind, which is
        not what the button says.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM issue_items WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM issues WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM saved WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM feeds WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM delivery WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM reads WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM emails WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM issue_skips WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM candidates WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM article_signals WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM archived_articles WHERE secret = ?", (secret,))
            conn.execute("DELETE FROM article_events WHERE secret = ?", (secret,))

    def rename_secret(self, old: str, new: str) -> None:
        """Move everything to a new secret when the catalogue URL is rotated.

        The credential changes; the catalogue does not. Without this a rotated
        URL opens an empty catalogue while the old rows sit unreachable."""
        with self._conn() as conn:
            for table in _SECRET_TABLES:
                conn.execute(f"UPDATE {table} SET secret = ? WHERE secret = ?", (new, old))

    # --------------------------------------------------------------- skips

    def skip_article(
        self, secret: str, article_id: str, title: str, section: str, url: str | None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO issue_skips (secret, article_id, title, section, url, skipped_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (secret, article_id) DO NOTHING",
                (secret, article_id, title[:300], section[:120], url, time.time()),
            )

    def unskip_article(self, secret: str, article_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM issue_skips WHERE secret = ? AND article_id = ?",
                (secret, article_id),
            )

    def list_skips(self, secret: str, *, max_age: float | None = None) -> list[SkippedArticle]:
        sql = "SELECT article_id, title, section, url FROM issue_skips WHERE secret = ?"
        params: list = [secret]
        if max_age is not None:
            sql += " AND skipped_at >= ?"
            params.append(time.time() - max_age)
        with self._conn() as conn:
            rows = conn.execute(f"{sql} ORDER BY skipped_at DESC", params).fetchall()
        return [
            SkippedArticle(
                article_id=r["article_id"], title=r["title"], section=r["section"], url=r["url"]
            )
            for r in rows
        ]

    def skipped_ids(self, secret: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT article_id FROM issue_skips WHERE secret = ?", (secret,)
            ).fetchall()
        return {r["article_id"] for r in rows}

    def has_issue_for_day(self, secret: str, local_day: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM issues WHERE secret = ? AND local_day = ?",
                (secret, local_day),
            ).fetchone()
        return row is not None

    def issued_articles(self, secret: str, article_ids: list[str]) -> set[str]:
        """Which of `article_ids` already appeared in an issue.

        Chunked because SQLite's parameter limit is 999 by default and a large
        feed set can exceed that in one call."""
        seen: set[str] = set()
        for i in range(0, len(article_ids), 500):
            chunk = article_ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT article_id FROM issue_items WHERE secret = ?"
                    f" AND article_id IN ({placeholders})",
                    (secret, *chunk),
                ).fetchall()
            seen.update(r["article_id"] for r in rows)
        return seen

    # ------------------------------------------------------------ delivery

    def set_delivery(
        self,
        secret: str,
        *,
        webhook_url: str | None,
        webhook_secret: str | None,
        email: str | None,
        schedule: str,
        timezone: str,
        enabled: bool,
    ) -> None:
        secret_blob = (
            self._fernet.encrypt(webhook_secret.encode()).decode() if webhook_secret else None
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO delivery (secret, webhook_url, webhook_secret, email,"
                " schedule, timezone, enabled) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (secret) DO UPDATE SET"
                " webhook_url = excluded.webhook_url,"
                " webhook_secret = excluded.webhook_secret,"
                " email = excluded.email,"
                " schedule = excluded.schedule,"
                " timezone = excluded.timezone,"
                " enabled = excluded.enabled",
                (
                    secret,
                    _blank_to_none(webhook_url),
                    secret_blob,
                    _blank_to_none(email),
                    schedule,
                    timezone,
                    int(enabled),
                ),
            )

    def ensure_delivery(
        self, secret: str, *, schedule: str, timezone: str, email: str | None = None
    ) -> None:
        """Create the delivery row on first use, leaving an existing one alone.

        The scheduler only sees catalogues with a row, so without this a
        catalogue that added feeds but never opened settings gets no issues."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO delivery (secret, email, schedule, timezone, enabled)"
                " VALUES (?, ?, ?, ?, 1) ON CONFLICT (secret) DO NOTHING",
                (secret, _blank_to_none(email), schedule, timezone),
            )

    def set_enabled(self, secret: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE delivery SET enabled = ? WHERE secret = ?", (int(enabled), secret)
            )

    def set_auto_select(self, secret: str, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE delivery SET auto_select = ? WHERE secret = ?",
                (int(enabled), secret),
            )

    def set_issue_limits(
        self,
        secret: str,
        *,
        max_articles: int | None,
        max_per_feed: int | None,
        max_words: int | None,
        window_hours: int | None,
        max_saved: int | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE delivery SET max_articles = ?, max_per_feed = ?, max_words = ?,"
                " window_hours = ?, max_saved = ? WHERE secret = ?",
                (max_articles, max_per_feed, max_words, window_hours, max_saved, secret),
            )

    def get_delivery(self, secret: str) -> Delivery | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM delivery WHERE secret = ?", (secret,)
            ).fetchone()
        return _delivery(row, self._fernet) if row else None

    def list_deliveries(self) -> list[Delivery]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM delivery WHERE enabled = 1").fetchall()
        return [_delivery(r, self._fernet) for r in rows]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing database.

    CREATE TABLE IF NOT EXISTS does nothing on a database that already has the
    table, so a column added in a later release has to be introduced here. Cheap
    enough to check on every start, and it keeps deployments a plain restart.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Every table keyed by the catalogue secret; a new one must be added here so
# rotating the URL carries it along.
_SECRET_TABLES = (
    "feeds",
    "saved",
    "issues",
    "issue_items",
    "emails",
    "reads",
    "delivery",
    "issue_skips",
    "candidates",
    "article_signals",
    "archived_articles",
    "article_events",
)


def _archive_id(url: str) -> str:
    return "arch:" + hashlib.sha1(url.encode()).hexdigest()[:16]


def _archived(row: sqlite3.Row) -> ArchivedArticle:
    return ArchivedArticle(
        id=row["id"],
        url=row["url"],
        article_id=row["article_id"],
        title=row["title"],
        source=row["source"],
        archived_at=row["archived_at"],
        kind=row["kind"],
    )


def _candidate(row: sqlite3.Row) -> CandidateItem:
    return CandidateItem(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        source=row["source"],
        summary=row["summary"],
        content=row["content"],
        published=row["published"],
        added_at=row["added_at"],
    )


def _feed(row: sqlite3.Row, fernet: Fernet) -> Feed:
    cookie: str | None = None
    if row["auth_cookie"]:
        try:
            cookie = fernet.decrypt(row["auth_cookie"].encode()).decode()
        except InvalidToken:
            # A changed app key loses the cookie, like the webhook secret. The
            # feed keeps working anonymously until it is re-entered.
            cookie = None
    return Feed(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        paused=bool(row["paused"]),
        last_fetched_at=row["last_fetched_at"],
        last_error=row["last_error"],
        item_count=row["item_count"],
        last_item_at=row["last_item_at"],
        auth_cookie=cookie,
    )


def _issue(row: sqlite3.Row) -> Issue:
    return Issue(
        id=row["id"],
        secret=row["secret"],
        title=row["title"],
        created_at=row["created_at"],
        path=row["path"],
        article_count=row["article_count"],
        word_count=row["word_count"],
    )


def _delivery(row: sqlite3.Row, fernet: Fernet) -> Delivery:
    webhook_secret: str | None = None
    if row["webhook_secret"]:
        try:
            webhook_secret = fernet.decrypt(row["webhook_secret"].encode()).decode()
        except InvalidToken:
            # Same failure mode as a changed app key on the token store: the
            # row survives, the secret is gone. Deliveries fail cleanly rather
            # than signing with an empty key, which the receiver would reject
            # anyway.
            webhook_secret = None
    return Delivery(
        secret=row["secret"],
        webhook_url=row["webhook_url"],
        webhook_secret=webhook_secret,
        email=row["email"],
        schedule=row["schedule"],
        timezone=row["timezone"],
        enabled=bool(row["enabled"]),
        max_articles=row["max_articles"],
        max_per_feed=row["max_per_feed"],
        max_words=row["max_words"],
        window_hours=row["window_hours"],
        max_saved=row["max_saved"],
        auto_select=bool(row["auto_select"]),
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
