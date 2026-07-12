"""send_magic_link routing: Resend > SMTP > stdout."""

from unittest.mock import MagicMock


def test_stdout_fallback_when_nothing_configured(capsys, monkeypatch):
    import app.config
    import app.mail
    monkeypatch.setattr(app.config, "RESEND_API_KEY", "")
    monkeypatch.setattr(app.config, "SMTP_HOST", "")

    app.mail.send_magic_link("dev@example.com", "http://localhost:8000/auth/verify?token=abc")
    out = capsys.readouterr().out
    assert "dev magic link" in out
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
    called.assert_called_once_with("alice@example.com", "https://x/verify?token=t")


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


def test_resend_send_calls_api_with_expected_params(monkeypatch):
    """_send_via_resend uses the Resend SDK shape from the docs."""
    import app.config
    import app.mail

    monkeypatch.setattr(app.config, "RESEND_API_KEY", "re_test_123")
    monkeypatch.setattr(app.config, "RESEND_FROM", "rssFriends <no-reply@rssfriends.com>")

    fake_resend = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "resend", fake_resend)

    app.mail._send_via_resend("charlie@example.com", "https://x/verify?token=t3")

    assert fake_resend.api_key == "re_test_123"
    fake_resend.Emails.send.assert_called_once()
    params = fake_resend.Emails.send.call_args[0][0]
    assert params["from"] == "rssFriends <no-reply@rssfriends.com>"
    assert params["to"] == ["charlie@example.com"]
    assert "rssFriends" in params["subject"]
    assert "token=t3" in params["html"]
    assert "token=t3" in params["text"]
