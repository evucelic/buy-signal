"""Report delivery order, failure fallback, Telegram rate limits and credential hygiene."""

from unittest.mock import MagicMock

import pytest
import requests

from buysignal.telegram_bot import transport
from buysignal.telegram_bot.messages import Report, ReportMessage


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(transport.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(transport, "_last_delivery_at", 0)


def test_report_sends_each_chart_after_its_explanation(monkeypatch):
    delivered = []
    monkeypatch.setattr(transport, "send", lambda text: delivered.append(("text", text)) or True)
    monkeypatch.setattr(transport, "send_photo", lambda png, caption: delivered.append(("photo", caption)) or True)
    report = Report([ReportMessage("summary"), ReportMessage("drawdown explanation", photo=b"png", caption="SPY"), ReportMessage("credit")])
    assert transport.send_report(report)
    assert delivered == [("text", "summary"), ("text", "drawdown explanation"), ("photo", "SPY"), ("text", "credit")]


def test_photo_failure_keeps_the_remaining_text_available(monkeypatch):
    delivered = []
    monkeypatch.setattr(transport, "send", lambda text: delivered.append(text) or True)
    monkeypatch.setattr(transport, "send_photo", lambda *args: False)
    assert transport.send_report(Report([ReportMessage("facts", photo=b"png"), ReportMessage("more facts")]))
    assert delivered == ["facts", "more facts"]


def test_text_failure_stops_the_report_and_returns_failure(monkeypatch):
    delivered = []
    monkeypatch.setattr(transport, "send", lambda text: delivered.append(text) and False)
    assert not transport.send_report(Report([ReportMessage("summary"), ReportMessage("details")]))
    assert delivered == ["summary"]


def response(status=200, payload=None):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = payload or {"ok": True}
    return result


def test_only_the_overview_notifies_the_chat(monkeypatch):
    payloads = []
    def post(url, json=None, **kwargs):
        payloads.append(dict(json))
        return response()
    monkeypatch.setattr(transport.requests, "post", post)
    assert transport.send_report(Report([ReportMessage("summary"), ReportMessage("details")]))
    assert [p["disable_notification"] for p in payloads] == [False, True]


def test_rate_limit_uses_telegram_retry_after(monkeypatch):
    waits = []
    replies = iter([response(429, {"ok": False, "parameters": {"retry_after": 3}}), response()])
    monkeypatch.setattr(transport.requests, "post", lambda *args, **kwargs: next(replies))
    monkeypatch.setattr(transport.time, "sleep", waits.append)
    assert transport.send("message")
    assert 3 in waits


def test_api_ok_false_is_not_reported_as_delivered(monkeypatch):
    monkeypatch.setattr(transport.requests, "post", lambda *args, **kwargs: response(payload={"ok": False}))
    assert not transport.send("message")


def test_bot_token_is_redacted_from_transport_errors(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise requests.exceptions.RequestException("failed at https://api.telegram.org/bottest-token/sendMessage")
    monkeypatch.setattr(transport.requests, "post", fail)
    assert not transport.send("message")
    assert "test-token" not in caplog.text
    assert "[redacted]" in caplog.text


def test_long_cooldown_does_not_retry_before_telegram_allows_it(monkeypatch):
    posted = []
    def post(*args, **kwargs):
        posted.append(True)
        return response(429, {"ok": False, "parameters": {"retry_after": 120}})
    monkeypatch.setattr(transport.requests, "post", post)
    assert not transport.send("message")
    assert len(posted) == 1


def test_batches_do_not_interleave_between_threads(monkeypatch):
    import threading
    entered, second_waiting, release = threading.Event(), threading.Event(), threading.Event()
    output = []
    def send(text):
        output.append(text)
        if text == "A1":
            entered.set()
            assert release.wait(2)
        return True
    monkeypatch.setattr(transport, "send", send)
    first = threading.Thread(target=lambda: transport.send_report(Report([ReportMessage("A1"), ReportMessage("A2")])))
    def second_batch():
        second_waiting.set()
        transport.send_report(Report([ReportMessage("B1"), ReportMessage("B2")]))
    second = threading.Thread(target=second_batch)
    first.start()
    assert entered.wait(2)
    second.start()
    assert second_waiting.wait(2)
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert output == ["A1", "A2", "B1", "B2"]


def test_poll_loop_delivers_prepared_report_replies(monkeypatch):
    from unittest.mock import Mock
    report = Report([ReportMessage("overview"), ReportMessage("details")])
    updates = response(payload={"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/signal"}}]})
    import threading
    delivered_event = threading.Event()
    def get(*args, **kwargs):
        if not get.called:
            get.called = True
            return updates
        assert delivered_event.wait(2)
        raise KeyboardInterrupt()
    get.called = False
    getter = get
    monkeypatch.setattr(transport.requests, "get", getter)
    delivered = []
    monkeypatch.setattr(transport, "send_report", lambda value: delivered.append(value) or delivered_event.set() or True)
    with pytest.raises(KeyboardInterrupt):
        transport.poll_forever(lambda text: report)
    assert delivered == [report]
