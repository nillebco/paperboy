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
                "SELECT id, url, title FROM feeds WHERE secret = ? ORDER BY added_at",
                (secret,),
            ).fetchall()
        return [Feed(id=r["id"], url=r["url"], title=r["title"]) for r in rows]

    def get_feed(self, secret: str, fid: str) -> Feed | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, url, title FROM feeds WHERE secret = ? AND id = ?",
                (secret, fid),
            ).fetchone()
        return Feed(id=row["id"], url=row["url"], title=row["title"]) if row else None

    def rename_feed(self, secret: str, fid: str, title: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE feeds SET title = ? WHERE secret = ? AND id = ?",
                (title, secret, fid),
            )

    def delete_feed(self, secret: str, fid: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM feeds WHERE secret = ? AND id = ?", (secret, fid)
            )
        return cur.rowcount > 0

    # -------------------------------------------------------- saved queue

    def save_url(
        self, secret: str, url: str, title: str | None = None, content: str | None = None
    ) -> bool:
        """Queue a link. True if it was new, False if already queued.

        Re-saving a consumed link is allowed and re-queues it: the user's intent
        is "put this back in", and silently ignoring that looks broken."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, consumed_by FROM saved WHERE secret = ? AND url = ?",
                (secret, url),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO saved (secret, url, title, added_at, content)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (secret, url, title, time.time(), content),
                )
                return True
            if row["consumed_by"] is not None:
                conn.execute(
                    "UPDATE saved SET consumed_by = NULL, added_at = ?, content = ?"
                    " WHERE id = ?",
                    (time.time(), content, row["id"]),
                )
                return True
            if content is not None:
                # Re-saving a queued link with body text upgrades it: the agent
                # may retry with more context than the first attempt had.
                conn.execute("UPDATE saved SET content = ? WHERE id = ?", (content, row["id"]))
                return True
            return False

    def list_saved(self, secret: str, include_consumed: bool = False) -> list[SavedItem]:
        sql = "SELECT id, url, title, added_at, content, consumed_by FROM saved WHERE secret = ?"
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
            )
            for r in rows
        ]

    def get_saved(self, secret: str, saved_id: int) -> SavedItem | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, url, title, added_at, content FROM saved"
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
    )


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
