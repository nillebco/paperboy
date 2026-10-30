"""Matins configuration, kept beside the upstream env handling.

Reads happen per call like `later_ink.config`, so tests can flip an env var
without reloading modules.
"""

import os
from pathlib import Path


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def get_issues_dir(database_path: str) -> Path:
    """Where composed issues are written. Defaults beside the database, which
    in Docker is the persistent volume — an issue that vanished on restart
    would be indistinguishable from a build that never ran."""
    raw = os.environ.get("ISSUES_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path(database_path).resolve().parent / "issues"


def get_issue_window_hours() -> int:
    """How far back a feed item may be and still make the issue.

    A day by default: the point is a newspaper of what is new since yesterday,
    not a backlog. Raise it for weekly issues."""
    return _int("ISSUE_WINDOW_HOURS", 24, minimum=1)


def get_issue_max_articles() -> int:
    return _int("ISSUE_MAX_ARTICLES", 15, minimum=1)


def get_issue_max_per_feed() -> int:
    """Per-feed cap. Without it one prolific feed becomes the whole paper."""
    return _int("ISSUE_MAX_PER_FEED", 4, minimum=1)


def get_issue_max_words() -> int:
    """Word budget for one issue — the thing that keeps it readable on a small
    screen in a morning, rather than a 3-hour download."""
    return _int("ISSUE_MAX_WORDS", 12000, minimum=500)


def get_issue_max_saved() -> int:
    return _int("ISSUE_MAX_SAVED", 10, minimum=0)


def get_issue_retention() -> int:
    """Issues kept per user on disk. 0 keeps everything."""
    return _int("ISSUE_RETENTION", 14, minimum=0)


def scheduler_enabled() -> bool:
    return os.environ.get("MATINS_SCHEDULER", "1").lower() in ("1", "true", "yes")


def get_scheduler_interval() -> float:
    return float(_int("MATINS_SCHEDULER_INTERVAL_SECONDS", 60, minimum=5))


def get_otp_per_hour() -> int:
    """Sign-in codes one address may request per hour. A person needs one or
    two; a loop needs to be stopped."""
    return _int("OTP_PER_HOUR", 5, minimum=1)


def get_otp_cooldown() -> float:
    return float(_int("OTP_COOLDOWN_SECONDS", 60, minimum=1))


def get_admin_token() -> str | None:
    return os.environ.get("ADMIN_TOKEN") or None


def get_default_schedule() -> str:
    return os.environ.get("DEFAULT_ISSUE_SCHEDULE", "07:00")


def get_default_timezone() -> str:
    return os.environ.get("DEFAULT_ISSUE_TIMEZONE", "Europe/Paris")


def webhook_allow_private() -> bool:
    """Allow webhook targets on private addresses.

    Off on a hosted instance: the URL is user-supplied, and a POST to
    169.254.169.254 or an internal admin port is exactly the SSRF this would
    otherwise hand out. A self-hoster pointing it at Home Assistant on the LAN
    turns it on knowingly.
    """
    return os.environ.get("WEBHOOK_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def get_smtp_config() -> dict[str, object] | None:
    """SMTP settings for notification email, or None when not configured.

    Email is notification only — a subject line and a link. The issue itself is
    never attached: it would mean maintaining a Send-to-Kindle path and a
    25 MB ceiling in exchange for something OPDS already does better.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        return None
    return {
        "host": host,
        "port": _int("SMTP_PORT", 587, minimum=1),
        "username": os.environ.get("SMTP_USERNAME", "").strip() or None,
        "password": os.environ.get("SMTP_PASSWORD", "") or None,
        "sender": sender,
        "tls": os.environ.get("SMTP_TLS", "starttls").strip().lower(),
    }


def get_feed_fetch_timeout() -> float:
    return float(_int("FEED_FETCH_TIMEOUT", 15, minimum=1))


def get_feed_max_bytes() -> int:
    return _int("FEED_MAX_BYTES", 5 * 1024 * 1024, minimum=64 * 1024)


def get_article_max_bytes() -> int:
    return _int("ARTICLE_MAX_BYTES", 4 * 1024 * 1024, minimum=64 * 1024)


def get_webhook_timeout() -> float:
    return float(_int("WEBHOOK_TIMEOUT", 10, minimum=1))


def get_webhook_attempts() -> int:
    return _int("WEBHOOK_ATTEMPTS", 3, minimum=1)
