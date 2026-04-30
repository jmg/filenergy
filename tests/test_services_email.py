import pytest

from filenergy.services import email as email_service


def test_log_adapter_returns_true_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "log")
    with caplog.at_level("INFO", logger="filenergy.services.email"):
        ok = email_service.send("a@b", "hi", "body")
    assert ok is True
    assert any("To=a@b" in r.message for r in caplog.records)


def test_unknown_adapter_returns_false(monkeypatch):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "carrier-pigeon")
    assert email_service.send("a@b", "x", "y") is False


def test_smtp_adapter_no_host_returns_false(monkeypatch):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "smtp")
    monkeypatch.delenv("FILENERGY_SMTP_HOST", raising=False)
    assert email_service.send("a@b", "x", "y") is False


def test_smtp_adapter_uses_smtplib(monkeypatch):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "smtp")
    monkeypatch.setenv("FILENERGY_SMTP_HOST", "mail.test")
    monkeypatch.setenv("FILENERGY_SMTP_USER", "u")
    monkeypatch.setenv("FILENERGY_SMTP_PASSWORD", "p")
    monkeypatch.setenv("FILENERGY_SMTP_TLS", "true")

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append({"host": host, "port": port})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent.append("tls")

        def login(self, user, password):
            sent.append({"login": user})

        def send_message(self, msg):
            sent.append({"to": msg["To"], "subject": msg["Subject"]})

    monkeypatch.setattr("filenergy.services.email.smtplib.SMTP", FakeSMTP)
    assert email_service.send("dst@x", "subj", "body") is True
    assert any(item == "tls" for item in sent)
    assert any(isinstance(i, dict) and i.get("subject") == "subj" for i in sent)


def test_smtp_adapter_handles_exception(monkeypatch):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "smtp")
    monkeypatch.setenv("FILENERGY_SMTP_HOST", "mail.test")

    class BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr("filenergy.services.email.smtplib.SMTP", BoomSMTP)
    assert email_service.send("dst@x", "s", "b") is False


def test_smtp_adapter_no_tls_no_auth(monkeypatch):
    monkeypatch.setenv("FILENERGY_EMAIL_ADAPTER", "smtp")
    monkeypatch.setenv("FILENERGY_SMTP_HOST", "mail.test")
    monkeypatch.setenv("FILENERGY_SMTP_TLS", "false")
    monkeypatch.delenv("FILENERGY_SMTP_USER", raising=False)
    monkeypatch.delenv("FILENERGY_SMTP_PASSWORD", raising=False)

    calls = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            calls.append("init")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            calls.append("tls")

        def send_message(self, msg):
            calls.append("send")

    monkeypatch.setattr("filenergy.services.email.smtplib.SMTP", FakeSMTP)
    assert email_service.send("a@b", "s", "b") is True
    assert "tls" not in calls
    assert "send" in calls
