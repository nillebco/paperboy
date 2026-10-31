"""Notification email — a nudge, never the newspaper.

The subject line and a link are the entire message. Attaching the issue would
force a Send-to-Kindle path, a size ceiling, and a second way for the same
content to reach a device that already speaks OPDS. Keeping email to a
notification also means SMTP can be slow, rate-limited or down without the
issue itself failing to exist.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from . import config

logger = logging.getLogger(__name__)


def issue_subject(issue) -> str:
    return f"Your issue is ready — {issue.article_count} articles"


def issue_body(issue, catalog_url: str, issue_url: str | None = None) -> str:
    minutes = max(1, round(issue.word_count / 250))
    issue_link = (
        f"Read or download this issue:\n{issue_url}\n\n" if issue_url else ""
    )
    return (
        f"{issue.title}\n\n"
        f"{issue.article_count} articles · about {minutes} minutes of reading.\n\n"
        f"{issue_link}"
        f"Your catalogue (EPUB / reader):\n{catalog_url}\n\n"
        "Nothing is attached: the issue lives on your server, and your reader "
        "pulls it.\n"
    )


async def send_code(recipient: str, code: str, *, smtp: dict | None = None) -> bool:
    """Send a sign-in code. Short, text-only, no link — a code email with a link
    trains people to click, which is the phishing habit this avoids."""
    smtp = smtp or config.get_smtp_config()
    if not smtp:
        logger.warning("no SMTP configured; cannot send a sign-in code")
        return False
    message = EmailMessage()
    message["From"] = str(smtp["sender"])
    message["To"] = recipient
    message["Subject"] = f"{code} is your Matins code"
    message.set_content(
        f"Your Matins sign-in code is {code}.\n\n"
        "It expires in 10 minutes and can be used once.\n\n"
        "If you did not ask for this, ignore the message — nobody can sign in "
        "without the code.\n"
    )
    try:
        await asyncio.to_thread(_send, smtp, recipient, message)
    except (OSError, smtplib.SMTPException) as e:
        logger.warning("code email to %s failed: %s", _mask(recipient), e)
        return False
    return True


async def send_issue_notification(
    recipient: str,
    issue,
    catalog_url: str,
    issue_url: str | None = None,
    *,
    smtp: dict | None = None,
) -> bool:
    smtp = smtp or config.get_smtp_config()
    if not smtp:
        return False
    message = EmailMessage()
    message["From"] = str(smtp["sender"])
    message["To"] = recipient
    message["Subject"] = issue_subject(issue)
    message.set_content(issue_body(issue, catalog_url, issue_url))
    # No HTML part: the text version is the whole message, and an HTML twin
    # would only add a place for a tracking pixel to be expected.
    try:
        await asyncio.to_thread(_send, smtp, recipient, message)
    except (OSError, smtplib.SMTPException) as e:
        logger.warning("notification email to %s failed: %s", _mask(recipient), e)
        return False
    return True


def _send(smtp: dict, recipient: str, message: EmailMessage) -> None:
    host = str(smtp["host"])
    port = int(smtp["port"])
    tls = str(smtp.get("tls") or "starttls")
    if tls == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
    try:
        client.ehlo()
        if tls == "starttls":
            client.starttls()
            client.ehlo()
        if smtp.get("username") and smtp.get("password"):
            client.login(str(smtp["username"]), str(smtp["password"]))
        client.send_message(message, to_addrs=[recipient])
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            # A server that hangs up after accepting the message is normal; the
            # send already succeeded and quitting is best-effort.
            client.close()


def _mask(address: str) -> str:
    """Logs keep the domain (useful for diagnosing a provider) but not the
    mailbox."""
    local, sep, domain = address.partition("@")
    if not sep:
        return "(redacted)"
    return f"{local[:1]}***@{domain}"
