"""Tests for schedule/market_hours.py: the NYSE session boundaries and the VIX window.

Pure calendar questions, asked with literal timestamps - no scheduling, no collectors, and
nothing to monkeypatch.
"""

from datetime import datetime

from buysignal.schedule import market_hours

ET = market_hours.ET
CT = market_hours.CT

# 2026-07-24 is a Friday (ordinary NYSE trading day, no holiday); 2026-07-25 is a Saturday.


def test_market_session_closed_on_weekend():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.CLOSED


def test_market_session_pre_market():
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.PRE_MARKET


def test_market_session_regular():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.REGULAR


def test_market_session_after_hours():
    dt = datetime(2026, 7, 24, 17, 0, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.AFTER_HOURS


def test_market_session_closed_late_night():
    dt = datetime(2026, 7, 24, 21, 0, tzinfo=ET)  # past close(16:00)+4h after-hours window
    assert market_hours.market_session(dt) == market_hours.CLOSED


def test_market_session_just_before_premarket_is_closed():
    dt = datetime(2026, 7, 24, 3, 59, 59, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.CLOSED


def test_market_session_exactly_at_premarket_start_is_pre_market():
    dt = datetime(2026, 7, 24, 4, 0, 0, tzinfo=ET)  # boundary is `<`, so == counts as started
    assert market_hours.market_session(dt) == market_hours.PRE_MARKET


def test_market_session_just_before_open_is_pre_market():
    dt = datetime(2026, 7, 24, 9, 29, 59, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.PRE_MARKET


def test_market_session_exactly_at_open_is_regular():
    dt = datetime(2026, 7, 24, 9, 30, 0, tzinfo=ET)  # boundary is `<`, so == is already REGULAR
    assert market_hours.market_session(dt) == market_hours.REGULAR


def test_market_session_just_before_close_is_regular():
    dt = datetime(2026, 7, 24, 15, 59, 59, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.REGULAR


def test_market_session_exactly_at_close_is_after_hours():
    dt = datetime(2026, 7, 24, 16, 0, 0, tzinfo=ET)  # boundary is `<`, so == is already AFTER_HOURS
    assert market_hours.market_session(dt) == market_hours.AFTER_HOURS


def test_market_session_just_before_after_hours_window_ends():
    dt = datetime(2026, 7, 24, 19, 59, 59, tzinfo=ET)
    assert market_hours.market_session(dt) == market_hours.AFTER_HOURS


def test_market_session_exactly_at_after_hours_window_end_is_closed():
    dt = datetime(2026, 7, 24, 20, 0, 0, tzinfo=ET)  # boundary is `>=`, so == is already CLOSED
    assert market_hours.market_session(dt) == market_hours.CLOSED


# --- vix_active_window ---------------------------------------------------------


def test_vix_active_window_just_before_start_is_inactive():
    dt = datetime(2026, 7, 24, 1, 59, tzinfo=CT)
    assert market_hours.vix_active_window(dt) is False


def test_vix_active_window_exactly_at_start_is_active():
    dt = datetime(2026, 7, 24, 2, 0, tzinfo=CT)  # boundary is `<=`, so == is already active
    assert market_hours.vix_active_window(dt) is True


def test_vix_active_window_just_before_end_is_active():
    dt = datetime(2026, 7, 24, 19, 59, tzinfo=CT)
    assert market_hours.vix_active_window(dt) is True


def test_vix_active_window_exactly_at_end_is_inactive():
    dt = datetime(2026, 7, 24, 20, 0, tzinfo=CT)  # boundary is `<`, so == is already inactive
    assert market_hours.vix_active_window(dt) is False


def test_vix_active_window_inactive_on_weekend_even_within_hour_range():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=CT)  # Saturday, well within 02:00-20:00
    assert market_hours.vix_active_window(dt) is False
