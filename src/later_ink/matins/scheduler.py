"""The scheduler: one loop, one job, no dependency.

An in-process asyncio loop rather than APScheduler/Celery. The work is "once a
day per user", the state that matters is in SQLite (which already records
whether today's issue exists), and a task queue would add an operational
component for something a 60-second tick does correctly.

Catch-up is deliberate: a job is due any time after its scheduled local time,
provided today's issue does not exist. A container restarted at 09:00 still
produces the 07:00 paper, late, instead of silently skipping the day.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import config
from .issues import local_day

logger = logging.getLogger(__name__)

# A build that produced nothing (every feed down, nothing new) would otherwise
# retry every tick for the rest of the day. Retrying half-hourly is enough for a
# transient outage and keeps the logs readable.
RETRY_AFTER = 1800.0

_attempts: dict[str, tuple[str, float]] = {}


async def run_forever(app) -> None:
    while True:
        try:
            await tick(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop must survive anything a single user's sources can throw:
            # one broken feed should not stop everyone's paper.
            logger.exception("scheduler tick failed")
        await asyncio.sleep(config.get_scheduler_interval())


async def tick(app, now: datetime | None = None) -> int:
    """Build and deliver every issue that is due. Returns how many were built."""
    store = getattr(app.state, "matins", None)
    service = getattr(app.state, "issues", None)
    if store is None or service is None:
        return 0

    now = now or datetime.now(UTC)
    built = 0

    for delivery in store.list_deliveries():
        day = local_day(delivery.timezone, now)
        if store.has_issue_for_day(delivery.secret, day):
            continue
        if not _due(delivery.schedule, delivery.timezone, now):
            continue
        if _attempted_recently(delivery.secret, day):
            continue
        _remember_attempt(delivery.secret, day)

        try:
            reader = await app.state.tenant_reader(delivery.secret)
        except Exception:
            logger.exception("could not open sources for %s", _mask(delivery.secret))
            continue

        try:
            issue = await service.build(
                secret=delivery.secret,
                reader=reader,
                local_day=day,
                now=now,
                only=None if delivery.auto_select else "queue",
            )
        except Exception:
            logger.exception("issue build failed for %s", _mask(delivery.secret))
            continue
        if issue is None:
            logger.info("nothing new for %s on %s", _mask(delivery.secret), day)
            continue

        built += 1
        try:
            await service.deliver(delivery.secret, issue)
        except Exception:
            logger.exception("delivery failed for %s", _mask(delivery.secret))

    return built


def _due(schedule: str, tz_name: str, now: datetime) -> bool:
    """True once the local clock has passed the scheduled time today.

    An unparseable schedule or timezone makes the user unreachable, so it is
    treated as "not due" and logged rather than guessed at — a wrong time is
    worse than none when the notification is the product."""
    try:
        hour, _, minute = schedule.partition(":")
        scheduled = int(hour) * 60 + int(minute or 0)
    except (ValueError, TypeError):
        logger.warning("ignoring unparseable schedule %r", schedule)
        return False
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        logger.warning("ignoring unknown timezone %r", tz_name)
        return False
    return local.hour * 60 + local.minute >= scheduled


def _attempted_recently(secret: str, day: str) -> bool:
    remembered = _attempts.get(secret)
    if remembered is None:
        return False
    remembered_day, when = remembered
    return remembered_day == day and time.monotonic() - when < RETRY_AFTER


def _remember_attempt(secret: str, day: str) -> None:
    _attempts[secret] = (day, time.monotonic())
    # Bounded so a long-running process with many deleted users does not keep
    # their secrets in memory forever. Entries are tiny; 5k is a ceiling, not a
    # tuning knob.
    while len(_attempts) > 5000:
        _attempts.pop(next(iter(_attempts)))


def _mask(secret: str) -> str:
    return f"catalogue …{secret[-4:]}" if len(secret) > 4 else "catalogue"
