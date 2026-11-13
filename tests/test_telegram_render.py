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
        "alert_state": None,
    }
    return render.RunnerStatus(**{**fields, **overrides})


# --- cache ages ---------------------------------------------------------------


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


# --- /status ---------------------------------------------------------------


def test_status_message_never_ticked():
    assert "never" in render.status_message(_status(), [], NOW)


def test_status_message_ok_after_tick():
    status = _status(last_tick_at=NOW, ok=True, alert_state="none")
    assert "(ok)" in render.status_message(status, [], NOW)


def test_status_message_failed_after_tick():
    status = _status(last_tick_at=NOW, ok=False, error="boom")
    assert "FAILED (boom)" in render.status_message(status, [], NOW)


def test_status_message_names_the_alert_state():
    status = _status(alert_state="strong")
    assert "Entry conditions aligned" in render.status_message(status, [], NOW)


def test_status_message_lists_each_collectors_last_attempt():
    statuses = [
        CollectorStatus("yield_curve", NOW - timedelta(hours=2), True, None),
        CollectorStatus("margin_debt", NOW, False, "cfbypass unreachable"),
    ]

    message = render.status_message(_status(), statuses, NOW)

    assert "collector" in message
    assert "yield_curve" in message
    assert "2h ago" in message
    assert "cfbypass unreachable" in message


def test_status_message_says_so_when_nothing_has_been_attempted():
    assert "no refresh attempted yet" in render.status_message(_status(), [], NOW)


def test_status_message_truncates_a_runaway_error():
    statuses = [CollectorStatus("fed_rate", NOW, False, "x" * 500)]

    assert "x" * 200 not in render.status_message(_status(), statuses, NOW)


# --- stale-collector notes ---------------------------------------------------------------


def test_stale_collector_notes_warns_when_a_collector_is_failing():
    statuses = [CollectorStatus("valuations", NOW, False, "ValueError: FY1 not found")]

    notes = render.stale_collector_notes("valuations", [], statuses)

    assert len(notes) == 1
    assert "FY1 not found" in notes[0]


def test_stale_collector_notes_stays_quiet_when_the_failure_was_already_reported():
    statuses = [CollectorStatus("valuations", NOW, False, "boom")]

    assert render.stale_collector_notes("valuations", [("valuations", "boom")], statuses) == []


def test_stale_collector_notes_stays_quiet_when_the_collector_is_healthy():
    statuses = [CollectorStatus("valuations", NOW, True, None)]

    assert render.stale_collector_notes("valuations", [], statuses) == []


def test_stale_collector_notes_stays_quiet_when_the_collector_is_unknown():
    assert render.stale_collector_notes("valuations", [], []) == []


def test_refresh_failures_lists_each_collector_and_escapes_it():
    block = render.refresh_failures([("fed_rate", "HTTPError: <503>")])

    assert "Refresh failed" in block
    assert "fed_rate" in block
    assert "&lt;503&gt;" in block


# --- /whattobuy ---------------------------------------------------------------


def test_opportunity_message_is_rendered_from_the_opportunity_alone(make_opportunity):
    message = render.opportunity_message(make_opportunity(), easing_obs_needed=1)

    assert "What to buy" in message
    assert "20.0" in message and "-25%" in message
    assert "Fed rate at the next meeting: lower (78%)" in message
    # Jargon and internal labels stay out of the message.
    for term in ("Small-cap band", "rate support", "relZ", "z-score"):
        assert term not in message


def test_opportunity_message_leads_with_the_owners_etfs_and_their_own_dips(make_opportunity):
    opp = make_opportunity()
    opp.segments[0].etf, opp.segments[2].etf = "VUAA", "VWCG"
    opp.segments[2].fwd_z = 1.4
    dips = {"VUAA": {"drawdown": -.031}, "VWCG": {"drawdown": 0., "refresh_failed": True}}
    message = render.opportunity_message(opp, 1, dips)
    table = message.split("<pre>")[1].split("</pre>")[0].splitlines()
    assert [line.split()[0] for line in table[1:]] == ["VUAA", "VWCG", "Small"]
    assert table[1].split() == ["VUAA", "20.0", "—", "-3.1%"]
    assert table[2].split() == ["VWCG", "16.0", "-20%", "at", "high"]
    assert max(map(len, table)) <= 33
    assert "Against its own past: VWCG cheap" in message
    assert "Dip from cached prices: VWCG" in message


def test_opportunity_message_names_the_buy_signal_context(make_opportunity):
    opp = make_opportunity()
    opp.buy_signal_state = "none"

    assert "Market read: No buy signal" in render.opportunity_message(opp, 1)
