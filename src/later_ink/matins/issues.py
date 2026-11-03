"""Selection and assembly of one day's issue.

The editorial rules are deliberately few and legible, because the failure mode
of an automated newspaper is not "no articles" — it is "forty articles, all from
the same site, none of them read". So: newest first, a per-feed cap, a word
budget for the whole issue, and a hard exclusion of anything already printed.

Everything here is idempotent per day: the scheduler asks "has this user got an
issue for today's local day?" before doing any work, and the dedupe table covers
the case where two runs race.
"""

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config as app_config
from ..connectors.base import ArticleUnavailable, UpstreamError
from ..epub import BuildResult
from . import config, notify, webhooks
from .db import Issue, MatinsStore
from .issue_epub import IssueItem, build_issue

logger = logging.getLogger(__name__)

SAVED_SECTION = "Saved for later"

# Used when a feed gives no word count at all. Low enough that a silent feed
# cannot monopolise the budget, high enough that it is not excluded for free.
DEFAULT_ITEM_WORDS = 400


@dataclass(frozen=True)
class Candidate:
    """One thing that would go into the next issue, before it is fetched."""

    source: str
    article: object
    section: str

    @property
    def estimated_words(self) -> int:
        return getattr(self.article, "word_count", None) or DEFAULT_ITEM_WORDS


@dataclass(frozen=True)
class IssueLimits:
    """The editorial rules for one catalogue: its overrides, else the instance's."""

    max_articles: int
    max_per_feed: int
    max_words: int
    window_hours: int
    max_saved: int


def limits_for(delivery) -> IssueLimits:
    return IssueLimits(
        max_articles=getattr(delivery, "max_articles", None) or config.get_issue_max_articles(),
        max_per_feed=getattr(delivery, "max_per_feed", None) or config.get_issue_max_per_feed(),
        max_words=getattr(delivery, "max_words", None) or config.get_issue_max_words(),
        window_hours=getattr(delivery, "window_hours", None) or config.get_issue_window_hours(),
        max_saved=getattr(delivery, "max_saved", None) or config.get_issue_max_saved(),
    )


class IssueService:
    def __init__(self, store: MatinsStore, issues_dir: Path):
        self.store = store
        self.issues_dir = Path(issues_dir)

    def issues_for(self, secret: str) -> list[Issue]:
        return self.store.list_issues(secret)

    def issue_path(self, secret: str, issue_id: str) -> Path:
        """Where one issue lives on disk.

        The directory is derived from the secret, and the filename from an id
        that came out of the database — never from the request path. A download
        route that built a filesystem path out of user input would be a
        traversal bug waiting for a clever id.
        """
        issue = self.store.get_issue(secret, issue_id)
        if issue is None:
            raise KeyError(issue_id)
        return Path(issue.path)

    async def build(
        self,
        *,
        secret: str,
        reader,
        local_day: str,
        now: datetime | None = None,
        only: str | None = None,
    ) -> Issue | None:
        now = now or datetime.now(UTC)
        limits = limits_for(self.store.get_delivery(secret))
        self.expire_queue(secret, now)
        # The whole queue, not the capped view: a link that cannot be fetched must
        # not cost a slot, so the cap is applied to what actually prints.
        candidates = await self.plan(secret, reader, now, only=only, whole_queue=True)
        logger.info("issue for %s on %s: %d candidates", _mask(secret), local_day, len(candidates))
        if not candidates:
            return None

        items: list[IssueItem] = []
        printed: list[Candidate] = []
        saved_printed = 0
        words_left = limits.max_words
        for candidate in candidates:
            source_name, article, section = candidate.source, candidate.article, candidate.section
            if source_name == "later" and saved_printed >= limits.max_saved:
                continue
            try:
                _article, html = await reader.get_article_html(
                    reader.decorate_id(source_name, article.id)
                )
            except (ArticleUnavailable, UpstreamError, KeyError) as e:
                # One dead link does not cancel the edition. The scheduled run
                # is the only chance the user gets today.
                logger.info("skipping %r for %s: %s", article.title, local_day, e)
                continue
            # Feeds rarely say how long an article is, so the word budget can only
            # be honest once the text is here. What does not fit waits in the queue.
            words = _words(html)
            delivery = self.store.get_delivery(secret)
            if items and words > words_left:
                logger.info("%r does not fit in %s's word budget", article.title, local_day)
                continue
            words_left -= words
            saved_printed += source_name == "later"
            items.append(
                IssueItem(
                    title=article.title,
                    html=html,
                    section=section,
                    author=article.author,
                    source_url=article.url,
                    image_url=article.image_url,
                    word_count=words,
                    published=published_label(
                        getattr(article, "published", None),
                        delivery.timezone if delivery else config.get_default_timezone(),
                    ),
                    show_dates=delivery.show_dates if delivery else True,
                )
            )
            printed.append(candidate)

        if not items:
            logger.info(
                "no item survived fetching for %s on %s", _mask(secret), local_day
            )
            return None

        title = f"Matins — {datetime.fromisoformat(local_day):%A %d %B %Y}"
        build = await build_issue(
            title=title,
            issue_date=now,
            items=items,
            language=_language(items),
        )
        issue_id = self.store.new_issue_id()
        path = self._write(secret, issue_id, build.result)
        word_count = sum(item.word_count for item in items)
        issue = self.store.record_issue(
            issue_id=issue_id,
            secret=secret,
            title=title,
            local_day=local_day,
            path=str(path),
            article_count=len(items),
            word_count=word_count,
            items=[
                (
                    reader.decorate_id(c.source, c.article.id),
                    c.article.title,
                    c.article.url,
                    c.section,
                )
                for c in printed
            ],
        )
        # Only mark consumed what actually made it into the file — a saved link
        # whose page failed to extract stays in the queue for tomorrow.
        saved_ids = [int(c.article.id) for c in printed if c.source == "later"]
        self.store.consume_saved(secret, saved_ids, issue_id)
        # Printed means done with: out of the reader's list, and out of the
        # curator's shortlist tomorrow. No "not for me" signal is recorded.
        self.store.archive_many(
            secret,
            [
                {
                    "url": c.article.url,
                    "article_id": reader.decorate_id(c.source, c.article.id),
                    "title": c.article.title,
                    "source": c.section,
                    "kind": "issue",
                }
                for c in printed
            ],
        )
        self._prune(secret)
        logger.info(
            "built issue %s for a catalogue: %d articles, %d words",
            issue_id,
            issue.article_count,
            issue.word_count,
        )
        return issue

    def expire_queue(self, secret: str, now: datetime | None = None) -> list[dict]:
        """Drop queued links that can no longer print: too old, or archived.

        An issue is the news of the last `window_hours`. A queued link is dated
        by its publication when the source stated one, else by when it was
        queued (a link shared from the phone is "new to me" from that moment).
        Anything archived in the meantime was dismissed, so it goes too."""
        now = now or datetime.now(UTC)
        limits = limits_for(self.store.get_delivery(secret))
        cutoff = now.timestamp() - limits.window_hours * 3600
        saved = self.store.list_saved(secret)
        archived = self.store.archived_urls(secret, [item.url for item in saved])
        expired: list[dict] = []
        for item in saved:
            reason = (
                "archived"
                if item.url in archived
                else "too old"
                if (item.published or item.added_at) < cutoff
                else None
            )
            if reason is None:
                continue
            self.store.delete_saved(secret, item.id)
            expired.append(
                {"title": item.title or item.url, "url": item.url,
                 "origin": item.origin, "reason": reason}
            )
        if expired:
            logger.info("expired %d queued links for %s", len(expired), _mask(secret))
        return expired

    def delete(self, secret: str, issue_id: str) -> bool:
        """Delete one edition. Its articles become eligible for the next one."""
        issue = self.store.get_issue(secret, issue_id)
        if issue is None:
            return False
        try:
            Path(issue.path).unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove %s", issue.path, exc_info=True)
        urls = [entry.url for entry in self.store.issue_entries(secret, issue_id) if entry.url]
        self.store.release_issue(secret, issue_id)
        self.store.unarchive_printed(secret, urls)
        return True

    def rename(self, old: str, new: str) -> None:
        """Carry a catalogue's data and issue files over to a rotated secret."""
        self.store.rename_secret(old, new)
        source = self._directory(old)
        target = self._directory(new)
        if source.exists():
            os.replace(source, target)
        for issue in self.store.list_issues(new, limit=10_000):
            self.store.set_issue_path(new, issue.id, str(target / Path(issue.path).name))

    def forget(self, secret: str) -> None:
        """Drop a catalogue's issues (files included) and its source settings.

        Called when a user deletes their catalogue. Issue files are named after
        a hash of the secret, so removing that directory is enough to take the
        composed EPUBs with it.
        """
        shutil.rmtree(self._directory(secret), ignore_errors=True)
        self.store.delete_user_data(secret)

    async def deliver(self, secret: str, issue: Issue) -> None:
        """Fire the webhook and the notification email, if configured.

        Runs after the issue is committed, so a failed delivery never means a
        missing issue — the reader can always pull it. Failures are logged, not
        raised: the caller is usually a scheduler loop that must keep going.
        """
        delivery = self.store.get_delivery(secret)
        if delivery is None:
            return
        catalog_url = f"{self._base_url()}/{secret}/issues/"
        issue_url = f"{catalog_url}{issue.id}"
        if delivery.webhook_url:
            payload = webhooks.issue_payload(issue, catalog_url)
            ok = await webhooks.deliver(
                delivery.webhook_url, delivery.webhook_secret, payload
            )
            if not ok:
                logger.warning("webhook for %s did not succeed", issue.id)
        email = delivery.email or self.store.email_for_secret(secret)
        if email:
            await notify.send_issue_notification(email, issue, catalog_url, issue_url)

    async def plan(
        self,
        secret: str,
        reader,
        now: datetime | None = None,
        only: str | None = None,
        whole_queue: bool = False,
    ) -> list[Candidate]:
        """What the next issue would contain, without fetching anything.

        Public because the preview page needs exactly this view: selection is
        opinionated, and the user should be able to see the picks before the
        service downloads and prints them.

        Saved items come first and are exempt from the per-feed cap: a link the
        user deliberately queued is a stronger signal than anything a feed
        pushed at them.

        `only="queue"` stops after the queue. An external curator (an agent that
        has already read the feeds and picked) composes an issue from exactly
        what it queued, instead of having the automatic feed selection add to
        it."""
        now = now or datetime.now(UTC)
        if only not in (None, "queue"):
            raise ValueError(f"unknown issue source filter: {only!r}")
        selected: list[Candidate] = []
        limits = limits_for(self.store.get_delivery(secret))
        budget = limits.max_words
        max_articles = limits.max_articles
        skipped = self.store.skipped_ids(secret)

        queue = reader.sources.get("later")
        if queue is not None:
            try:
                saved, _ = await queue.list_articles("inbox")
            except (UpstreamError, KeyError):
                saved = []
            # Same rule as expire_queue, without deleting: the preview must not
            # promise a link that composing would drop.
            archived = self.store.archived_urls(secret, [a.url for a in saved if a.url])
            saved = [
                a
                for a in saved
                if a.url not in archived
                and _queued_fresh(a, now, limits.window_hours)
            ]
            for article in saved if whole_queue else saved[: limits.max_saved]:
                cost = article.word_count or DEFAULT_ITEM_WORDS
                if not whole_queue and selected and budget - cost < 0:
                    break
                selected.append(Candidate("later", article, SAVED_SECTION))
                budget -= cost

        feeds = None if only == "queue" else reader.sources.get("rss")
        if feeds is not None:
            try:
                folders = await feeds.list_folders()
            except UpstreamError:
                folders = []
            await feeds.refresh()
            for folder in folders:
                if len(selected) >= max_articles or budget <= 0:
                    break
                try:
                    articles, _ = await feeds.list_articles(folder.id)
                except UpstreamError as e:
                    logger.info("feed %s unavailable: %s", folder.title, e)
                    continue
                # Dedupe keys are the catalogue-wide ids (prefixed), not the
                # raw connector ids — see TenantReader.decorate_id. Comparing
                # raw ids against the stored prefixed ones silently reprints
                # every article every day.
                keys = {a.id: reader.decorate_id("rss", a.id) for a in articles}
                # The catalogue adopted the feed's own title while parsing it
                # above (RssConnector._load renames an untitled subscription), so
                # re-read it: the Folder we listed a moment ago still carries the
                # URL, and that URL would otherwise become this section's name in
                # both the preview and the issue's table of contents.
                stored = self.store.get_feed(secret, folder.id)
                if stored is not None and stored.paused:
                    continue
                section = (stored.title if stored else "") or folder.title
                already = self.store.issued_articles(secret, list(keys.values()))
                archived = self.store.archived_urls(
                    secret, [a.url for a in articles if a.url]
                )
                fresh = [
                    a
                    for a in articles
                    if keys[a.id] not in already
                    and keys[a.id] not in skipped
                    and a.url not in archived
                    and _fresh(a, now, limits.window_hours)
                ]
                per_feed = 0
                for article in fresh:
                    if per_feed >= limits.max_per_feed:
                        break
                    if len(selected) >= max_articles:
                        break
                    cost = article.word_count or DEFAULT_ITEM_WORDS
                    if budget - cost < 0:
                        break
                    selected.append(Candidate("rss", article, section))
                    budget -= cost
                    per_feed += 1

        return selected

    def _write(self, secret: str, issue_id: str, result: BuildResult) -> Path:
        directory = self._directory(secret)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{issue_id}.epub"
        # Write beside the target and rename: a reader that opens the catalogue
        # mid-build must never see a half-written EPUB.
        tmp = directory / f".{issue_id}.tmp"
        tmp.write_bytes(result.data)
        os.replace(tmp, path)
        return path

    def _directory(self, secret: str) -> Path:
        return self.issues_dir / hashlib.sha1(secret.encode()).hexdigest()[:16]

    def _prune(self, secret: str) -> None:
        retention = config.get_issue_retention()
        if retention <= 0:
            return
        issues = self.store.list_issues(secret, limit=10_000)
        for issue in issues[retention:]:
            try:
                Path(issue.path).unlink(missing_ok=True)
            except OSError:
                logger.debug("could not remove %s", issue.path, exc_info=True)
            self.store.delete_issue(secret, issue.id)

    def _base_url(self) -> str:
        return app_config.get_base_url()


def published_label(published: datetime | None, tz_name: str) -> str:
    """A publication date in the reader's timezone, or nothing: never a stand-in."""
    if published is None:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = UTC
    moment = published.replace(tzinfo=UTC).astimezone(tz)
    return f"{moment.day} {moment:%b %Y}"


def local_day(tz_name: str, now: datetime | None = None) -> str:
    """The user's calendar day, as YYYY-MM-DD.

    Computed in their timezone, not the server's: "today's paper" is a local
    claim, and a UTC server would roll the day over mid-morning in Auckland."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(config.get_default_timezone())
    return now.astimezone(tz).date().isoformat()


def next_run(
    schedule: str, tz_name: str, *, issued_today: bool, now: datetime | None = None
) -> str:
    """When the next scheduled issue is due, in words: "Tomorrow at 07:00".

    Mirrors the scheduler's catch-up rule: past the scheduled time with no issue
    today, it is due at the next tick rather than tomorrow."""
    now = now or datetime.now(UTC)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(config.get_default_timezone())
    local = now.astimezone(tz)
    hour, _, minute = schedule.partition(":")
    try:
        scheduled = local.replace(hour=int(hour), minute=int(minute or 0))
    except ValueError:
        return f"At {schedule}"
    if issued_today:
        return f"Tomorrow at {scheduled:%H:%M}"
    if local < scheduled:
        return f"Today at {scheduled:%H:%M}"
    return "Due now, within a minute or two"


def _fresh(article, now: datetime, hours: int) -> bool:
    if article.content_date is None:
        return True  # a feed that dates nothing still deserves to be read
    reference = now.replace(tzinfo=None)
    return (reference - article.content_date).total_seconds() <= hours * 3600


def _queued_fresh(article, now: datetime, hours: int) -> bool:
    """A queued link: its publication date if known, else when it was queued."""
    reference = article.published or article.content_date
    if reference is None:
        return True
    return (now.replace(tzinfo=None) - reference).total_seconds() <= hours * 3600


def _words(html: str) -> int:
    from .extract import word_count

    return word_count(html)


def _language(items: list[IssueItem]) -> str:
    return "en"


def _mask(secret: str) -> str:
    """Logs name a catalogue without carrying its secret."""
    return f"catalogue …{secret[-4:]}" if len(secret) > 4 else "catalogue"
