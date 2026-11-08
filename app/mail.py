"""Email delivery — magic-link auth + follow notifications.

Three delivery paths, picked at call time from env config (highest
priority first):

  1. Resend HTTP API — set RSSFRIENDS_RESEND_API_KEY.
  2. SMTP — set RSSFRIENDS_SMTP_HOST.
  3. Dev mode — nothing configured; the message prints to stdout
     so a developer can inspect it without setting up email.

All senders go through send_email(), which catches exceptions and
logs them so a broken email backend never breaks a user action.
"""

import logging
import smtplib
from email.message import EmailMessage

from . import config

log = logging.getLogger(__name__)


def _from_address() -> str:
    """Preferred From: whichever provider is active, fall back to a
    generic value if neither is set (dev mode only)."""
    if config.RESEND_API_KEY:
        return config.RESEND_FROM
    if config.SMTP_HOST:
        return config.SMTP_FROM
    return "rssFriends <no-reply@rssfriends.local>"


def _send_via_resend(email: str, subject: str, text: str, html: str) -> None:
    import resend  # noqa: PLC0415 — lazy so folks without Resend don't need the dep

    resend.api_key = config.RESEND_API_KEY
    params = {
        "from": config.RESEND_FROM,
        "to": [email],
        "subject": subject,
        "text": text,
    }
    if html:
        params["html"] = html
    resend.Emails.send(params)
    log.info("email sent via resend to %s subject=%r", email, subject)


def _send_via_smtp(email: str, subject: str, text: str, html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = email
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
        if config.SMTP_STARTTLS:
            s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    log.info("email sent via smtp to %s subject=%r", email, subject)


def send_email(email: str, subject: str, text: str, html: str = "") -> None:
    """One entry point for every outbound email. Never raises — a
    broken email backend must not break the user action that triggered
    the send (e.g. following someone still succeeds even if the notify
    email fails)."""
    try:
        if config.RESEND_API_KEY:
            _send_via_resend(email, subject, text, html)
            return
        if config.SMTP_HOST:
            _send_via_smtp(email, subject, text, html)
            return
        # Dev mode.
        print(
            f"\n[dev email] to={email}\n  from={_from_address()}\n"
            f"  subject={subject}\n  {text}\n",
            flush=True,
        )
    except Exception:
        log.exception("send_email failed (to=%s subject=%r)", email, subject)


# ---------- Templates for specific message types ------------------------


def send_magic_link(email: str, url: str) -> None:
    subject = "Your rssFriends sign-in link"
    text = (
        f"Click to sign in to rssFriends:\n\n{url}\n\n"
        f"This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes."
    )
    html = (
        "<p>Click to sign in to rssFriends:</p>"
        f'<p><a href="{url}">{url}</a></p>'
        f"<p>This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes.</p>"
    )
    send_email(email, subject, text, html)


def send_invite(email: str, url: str, inviter_handle: str) -> None:
    from html import escape

    name = config.INSTANCE_NAME
    base = config.BASE_URL.rstrip("/")
    subject = f"@{inviter_handle} invited you to {name}"
    text = (
        f"@{inviter_handle} invited you to {name}, a social RSS radar.\n\n"
        f"Sign in to accept:\n\n{url}\n\n"
        f"This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes. If it has, "
        f"go to {base} and enter this email address to get a fresh one — "
        "the invite itself doesn't expire."
    )
    html = (
        f"<p>@{escape(inviter_handle)} invited you to {escape(name)}, a social RSS radar.</p>"
        f'<p><a href="{url}">Sign in to accept</a></p>'
        f"<p>This link expires in {config.MAGIC_LINK_TTL_MINUTES} minutes. If it has, "
        f'go to <a href="{base}">{escape(base)}</a> and enter this email address to '
        "get a fresh one — the invite itself doesn't expire.</p>"
    )
    send_email(email, subject, text, html)


def send_new_follower(followee_email: str, followee_handle: str,
                      follower_handle: str, approved: bool) -> None:
    """`approved=True` means auto-approved (public/unlisted followee);
    `approved=False` means a pending request landed in the private
    user's inbox."""
    base = config.BASE_URL.rstrip("/")
    follower_url = f"{base}/u/{follower_handle}"
    if approved:
        subject = f"@{follower_handle} started following you on rssFriends"
        text = (
            f"Hi @{followee_handle},\n\n"
            f"@{follower_handle} started following you.\n"
            f"See their radar: {follower_url}\n"
        )
        html = (
            f"<p>Hi @{followee_handle},</p>"
            f'<p><strong>@{follower_handle}</strong> started following you.</p>'
            f'<p><a href="{follower_url}">See their radar</a></p>'
        )
    else:
        subject = f"@{follower_handle} wants to follow your private radar"
        text = (
            f"Hi @{followee_handle},\n\n"
            f"@{follower_handle} requested to follow your private radar.\n"
            f"Approve or reject: {base}/follow-requests\n"
            f"Their radar: {follower_url}\n"
        )
        html = (
            f"<p>Hi @{followee_handle},</p>"
            f'<p><strong>@{follower_handle}</strong> requested to follow your private radar.</p>'
            f'<p><a href="{base}/follow-requests">Approve or reject the request</a></p>'
            f'<p><a href="{follower_url}">See their radar</a></p>'
        )
    send_email(followee_email, subject, text, html)


def send_suggestion_notification(
    collection_slug: str,
    collection_name: str,
    feed_url: str,
    note: str,
    suggester_handle: str | None,
) -> None:
    """Notify curator(s) that a new suggestion is waiting in the queue.

    Recipients from `config.SUGGESTION_NOTIFY_EMAILS`; the whole call
    is a no-op when `SUGGESTION_NOTIFY_ENABLED` is False. Every mail
    error is logged, not raised — a broken mail backend must not
    break the user's suggest action."""
    if not config.SUGGESTION_NOTIFY_ENABLED:
        return
    base = config.BASE_URL.rstrip("/")
    manage_url = f"{base}/collections/{collection_slug}/manage"
    subject = f"[rssFriends] New suggestion for “{collection_name}”"
    who = f"@{suggester_handle}" if suggester_handle else "someone"
    text = (
        f"{who} suggested a feed for the “{collection_name}” collection.\n\n"
        f"URL:   {feed_url}\n"
        f"Note:  {note or '(none)'}\n\n"
        f"Review + accept/reject:\n{manage_url}\n"
    )
    html = (
        f"<p><strong>{who}</strong> suggested a feed for "
        f"<em>{collection_name}</em>.</p>"
        f'<p>URL: <code>{feed_url}</code></p>'
        f"<p>Note: {note or '<em>(none)</em>'}</p>"
        f'<p><a href="{manage_url}">Review + accept/reject in the queue →</a></p>'
    )
    for addr in config.SUGGESTION_NOTIFY_EMAILS:
        send_email(addr, subject, text, html)


def send_collection_request_notification(
    name: str,
    description: str,
    language: str,
    locale: str,
    example_feeds: str,
    requester_handle: str,
) -> None:
    """0.11.4: notify admins that a user requested a new Collection
    (not a feed inside an existing one — a whole new collection)."""
    if not config.SUGGESTION_NOTIFY_ENABLED:
        return
    base = config.BASE_URL.rstrip("/")
    admin_url = f"{base}/collections/new"
    subject = f"[rssFriends] New collection request: “{name}”"
    text = (
        f"@{requester_handle} requested a new collection.\n\n"
        f"Name:        {name}\n"
        f"Language:    {language or '(unspecified)'}\n"
        f"Locale:      {locale or '(unspecified)'}\n"
        f"Description: {description or '(none)'}\n\n"
        f"Example feeds:\n{example_feeds or '(none)'}\n\n"
        f"Create it here (or reject by ignoring):\n{admin_url}\n"
    )
    for addr in config.SUGGESTION_NOTIFY_EMAILS:
        send_email(addr, subject, text, "")


def send_follow_approved(follower_email: str, follower_handle: str,
                         followee_handle: str) -> None:
    base = config.BASE_URL.rstrip("/")
    followee_url = f"{base}/u/{followee_handle}"
    subject = f"@{followee_handle} approved your follow request"
    text = (
        f"Hi @{follower_handle},\n\n"
        f"@{followee_handle} approved your follow request. You can now see "
        f"their radar and their activity in your Timeline.\n\n"
        f"{followee_url}\n"
    )
    html = (
        f"<p>Hi @{follower_handle},</p>"
        f'<p><strong>@{followee_handle}</strong> approved your follow request. '
        f"You can now see their radar and their activity in your Timeline.</p>"
        f'<p><a href="{followee_url}">Open their radar</a></p>'
    )
    send_email(follower_email, subject, text, html)
