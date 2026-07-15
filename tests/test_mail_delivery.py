"""Mail delivery routing: Resend > SMTP > stdout, for every message type."""

from unittest.mock import MagicMock


def test_stdout_fallback_when_nothing_configured(capsys, monkeypatch):
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "")

    app.mail.send_magic_link("dev@example.com", "http://localhost:8000/auth/verify?token=abc")
    out = capsys.readouterr().out
    assert "dev email" in out
    assert "dev@example.com" in out
    assert "token=abc" in out


def test_resend_wins_over_smtp_when_key_present(monkeypatch):
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "re_test_123")
    monkeypatch.setattr(app.config, "SMTP_HOST", "smtp.example.com")

    called = MagicMock()
    boom = MagicMock(side_effect=AssertionError("should not run"))
    monkeypatch.setattr(app.mail, "_send_via_resend", called)
    monkeypatch.setattr(app.mail, "_send_via_smtp", boom)

    app.mail.send_magic_link("alice@example.com", "https://x/verify?token=t")
    called.assert_called_once()


def test_smtp_used_when_only_smtp_configured(monkeypatch):
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "smtp.example.com")

    called = MagicMock()
    boom = MagicMock(side_effect=AssertionError("should not run"))
    monkeypatch.setattr(app.mail, "_send_via_smtp", called)
    monkeypatch.setattr(app.mail, "_send_via_resend", boom)

    app.mail.send_magic_link("bob@example.com", "https://x/verify?token=t2")
    called.assert_called_once()


def test_send_email_never_raises_when_backend_fails(monkeypatch, caplog):
    """A broken backend must NOT break the user action that triggered
    the send (e.g. following someone still succeeds even if the notify
    email fails)."""
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "re_test_123")

    def boom(*a, **kw):
        raise RuntimeError("resend is down")
    monkeypatch.setattr(app.mail, "_send_via_resend", boom)

    # Must not raise.
    app.mail.send_email("dev@example.com", "subject", "body")


def test_new_follower_email_content(monkeypatch, capsys):
    """The auto-approved case names the follower and links to their radar."""
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "")
    monkeypatch.setattr(app.config, "BASE_URL", "https://rssfriends.com")

    app.mail.send_new_follower(
        followee_email="alice@example.com",
        followee_handle="alice",
        follower_handle="bob",
        approved=True,
    )
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert "@bob started following you" in out
    assert "https://rssfriends.com/u/bob" in out


def test_pending_follow_email_points_to_requests_inbox(monkeypatch, capsys):
    """A pending request tells the private user to open the inbox."""
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "")
    monkeypatch.setattr(app.config, "BASE_URL", "https://rssfriends.com")

    app.mail.send_new_follower(
        followee_email="alice@example.com",
        followee_handle="alice",
        follower_handle="bob",
        approved=False,
    )
    out = capsys.readouterr().out
    assert "wants to follow" in out.lower() or "requested to follow" in out.lower()
    assert "/follow-requests" in out


def test_follow_approved_email_content(monkeypatch, capsys):
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "")
    monkeypatch.setattr(app.config, "BASE_URL", "https://rssfriends.com")

    app.mail.send_follow_approved(
        follower_email="bob@example.com",
        follower_handle="bob",
        followee_handle="alice",
    )
    out = capsys.readouterr().out
    assert "bob@example.com" in out
    assert "@alice approved" in out
    assert "https://rssfriends.com/u/alice" in out
