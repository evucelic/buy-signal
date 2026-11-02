"""Tests for telegram_bot/transport.py: the wire, and what it does when the wire is down.

Credentials are set per-test rather than globally: nothing in this package reads the
environment at import any more, so only the tests that actually send need them.
"""

import pytest
import requests

from buysignal.telegram_bot import transport


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(transport, "_last_delivery_at", 0)


def test_notify_started(monkeypatch):
    sent = []
    monkeypatch.setattr(transport, "send", lambda t: sent.append(t))
    transport.notify_started()
    assert sent == ["🟢 Runner started"]


def test_notify_stopped(monkeypatch):
    sent = []
    monkeypatch.setattr(transport, "send", lambda t: sent.append(t))
    transport.notify_stopped()
    assert sent == ["🔴 Runner stopped"]


def test_send_posts_the_message_to_the_configured_chat(monkeypatch):
    posted = {}

    def fake_post(url, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return _ok()

    monkeypatch.setattr(transport.requests, "post", fake_post)

    transport.send("hello")

    assert posted["url"].endswith("/bottest-token/sendMessage")
    assert posted["json"]["chat_id"] == "12345"
    assert posted["json"]["text"] == "hello"


def test_send_photo_posts_the_png(monkeypatch):
    posted = {}

    def fake_post(url, data=None, files=None, timeout=None):
        posted["url"] = url
        posted["files"] = files
        return _ok()

    monkeypatch.setattr(transport.requests, "post", fake_post)

    transport.send_photo(b"\x89PNG", "a caption")

    assert posted["url"].endswith("/sendPhoto")
    assert posted["files"]["photo"][1] == b"\x89PNG"


def test_send_swallows_request_exception(monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.exceptions.RequestException("fail")

    monkeypatch.setattr(transport.requests, "post", boom)
    transport.send("hi")  # must not raise
    assert "Telegram sendMessage failed" in caplog.text


def test_send_photo_swallows_request_exception(monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.exceptions.RequestException("fail")

    monkeypatch.setattr(transport.requests, "post", boom)
    transport.send_photo(b"", "caption")  # must not raise
    assert "Telegram sendPhoto failed" in caplog.text


def test_start_survives_a_failing_command_registration(monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.exceptions.RequestException("fail")

    threads = []
    monkeypatch.setattr(transport.requests, "post", boom)
    monkeypatch.setattr(transport.threading, "Thread", lambda **kw: _FakeThread(threads, **kw))

    transport.start(lambda text: None)

    assert "setMyCommands failed" in caplog.text
    assert threads == ["started"]  # polling starts anyway


class _FakeThread:
    def __init__(self, started, target=None, args=(), daemon=None):
        self._started = started

    def start(self):
        self._started.append("started")


def _ok():
    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

        def raise_for_status(self):
            return None

    return _Resp()
