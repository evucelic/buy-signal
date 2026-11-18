"""Tests for telegram_bot/render.py: values in, Telegram HTML out.

Every input is a literal - no clock, no cache files, no journal, no credentials - which is the
whole point of the module: rendering is the part of the bot you can check by reading it.
"""

from datetime import datetime, timedelta, timezone

from buysignal.schedule.journal import CollectorStatus
from buysignal.telegram_bot import render

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

def _status(**overrides):
    fields = {
        "uptime": timedelta(hours=3),
        "last_tick_at": None,
        "ok": True,
        "error": None,
        "read": None,
    }
    return render.RunnerStatus(**{**fields, **overrides})


# cache ages


def test_humanize_age_no_cache():
    assert render.humanize_age(None, NOW) == "no cache"


def test_humanize_age_just_now():
    assert render.humanize_age(NOW, NOW) == "just now"


def test_humanize_age_minutes():
    assert render.humanize_age(NOW - timedelta(minutes=5), NOW) == "5m ago"


def test_humanize_age_hours():
    assert render.humanize_age(NOW - timedelta(hours=3), NOW) == "3h ago"


def test_humanize_age_days():
    assert render.humanize_age(NOW - timedelta(days=2), NOW) == "2d ago"


# /status


def test_status_message_never_ticked():
    assert "never" in render.status_message(_status(), [], NOW)


def test_status_message_ok_after_tick():
    status = _status(last_tick_at=NOW, ok=True, read="Calm, macro sound")
    assert "(ok)" in render.status_message(status, [], NOW)


def test_status_message_failed_after_tick():
    status = _status(last_tick_at=NOW, ok=False, error="boom")
    assert "FAILED (boom)" in render.status_message(status, [], NOW)


def test_status_message_names_the_read():
    status = _status(read="Fear, macro deteriorating")
    assert "read        Fear, macro deteriorating" in render.status_message(status, [], NOW)


def test_status_message_lists_each_collectors_last_attempt():
    statuses = [
        CollectorStatus("yield_curve", NOW - timedelta(hours=2), True, None),
        CollectorStatus("margin_debt", NOW, False, "HTTPError: 503"),
    ]

    message = render.status_message(_status(), statuses, NOW)

    assert "collector" in message
    assert "yield_curve" in message
    assert "2h ago" in message
    assert "HTTPError: 503" in message


def test_status_message_says_so_when_nothing_has_been_attempted():
    assert "no refresh attempted yet" in render.status_message(_status(), [], NOW)


def test_status_message_truncates_a_runaway_error():
    statuses = [CollectorStatus("fed_rate", NOW, False, "x" * 500)]

    assert "x" * 200 not in render.status_message(_status(), statuses, NOW)


# stale-collector notes


def test_refresh_failures_lists_each_collector_and_escapes_it():
    block = render.refresh_failures([("fed_rate", "HTTPError: <503>")])

    assert "Refresh failed" in block
    assert "fed_rate" in block
    assert "&lt;503&gt;" in block
