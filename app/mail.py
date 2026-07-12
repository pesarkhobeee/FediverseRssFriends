"""Magic-link delivery.

Dev mode (no SMTP_HOST configured): prints the link to stdout so you can
click it without setting up email. Prod mode: plain-text email via SMTP
with optional STARTTLS.
"""

import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger(__name__)


def send_magic_link(email: str, url: str) -> None:
    if not config.SMTP_HOST:
        # Dev mode — surface the link so the developer can click it.
        print(f"\n[dev magic link] to={email}\n  {url}\n", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = "Your rssFriends sign-in link"
    msg["From"] = config.SMTP_FROM
    msg["To"] = email
    msg.set_content(
        f"Click to sign in to rssFriends:\n\n{url}\n\n"
        f"This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes."
    )

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
        if config.SMTP_STARTTLS:
            s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    log.info("magic link sent to %s", email)
