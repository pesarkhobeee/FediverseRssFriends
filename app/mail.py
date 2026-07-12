"""Magic-link delivery.

Three delivery paths, picked at call time from env config (highest
priority first):

  1. Resend HTTP API — set RSSFRIENDS_RESEND_API_KEY.
  2. SMTP — set RSSFRIENDS_SMTP_HOST.
  3. Dev mode — nothing configured; the link is printed to stdout
     so a developer can click it without setting up email.
"""

import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger(__name__)


def _subject() -> str:
    return "Your rssFriends sign-in link"


def _text_body(url: str) -> str:
    return (
        f"Click to sign in to rssFriends:\n\n{url}\n\n"
        f"This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes."
    )


def _html_body(url: str) -> str:
    return (
        "<p>Click to sign in to rssFriends:</p>"
        f'<p><a href="{url}">{url}</a></p>'
        f"<p>This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes.</p>"
    )


def _send_via_resend(email: str, url: str) -> None:
    # Lazy import so folks who don't use Resend don't need the package.
    import resend  # noqa: PLC0415

    resend.api_key = config.RESEND_API_KEY
    params = {
        "from": config.RESEND_FROM,
        "to": [email],
        "subject": _subject(),
        "html": _html_body(url),
        "text": _text_body(url),
    }
    resend.Emails.send(params)
    log.info("magic link sent via resend to %s", email)


def _send_via_smtp(email: str, url: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = _subject()
    msg["From"] = config.SMTP_FROM
    msg["To"] = email
    msg.set_content(_text_body(url))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
        if config.SMTP_STARTTLS:
            s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    log.info("magic link sent via smtp to %s", email)


def send_magic_link(email: str, url: str) -> None:
    if config.RESEND_API_KEY:
        _send_via_resend(email, url)
        return
    if config.SMTP_HOST:
        _send_via_smtp(email, url)
        return
    # Dev mode — surface the link so the developer can click it.
    print(f"\n[dev magic link] to={email}\n  {url}\n", flush=True)
