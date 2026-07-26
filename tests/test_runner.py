"""Tests for runner.py: session classification, tick's refresh-trigger conditions, and
run_forever()'s failure resilience.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import runner

ET = ZoneInfo("America/New_York")

# 2026-07-24 is a Friday (ordinary NYSE trading day, no holiday); 2026-07-25 is a Saturday.


def test_market_session_closed_on_weekend():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
    assert runner.market_session(dt) == runner.CLOSED


def test_market_session_pre_market():
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    assert runner.market_session(dt) == runner.PRE_MARKET


def test_market_session_regular():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    assert runner.market_session(dt) == runner.REGULAR


def test_market_session_after_hours():
    dt = datetime(2026, 7, 24, 17, 0, tzinfo=ET)
    assert runner.market_session(dt) == runner.AFTER_HOURS


def test_market_session_closed_late_night():
    dt = datetime(2026, 7, 24, 21, 0, tzinfo=ET)  # past close(16:00)+4h after-hours window
    assert runner.market_session(dt) == runner.CLOSED


def test_market_session_just_before_premarket_is_closed():
    dt = datetime(2026, 7, 24, 3, 59, 59, tzinfo=ET)
    assert runner.market_session(dt) == runner.CLOSED


def test_market_session_exactly_at_premarket_start_is_pre_market():
    dt = datetime(2026, 7, 24, 4, 0, 0, tzinfo=ET)  # boundary is `<`, so == counts as started
    assert runner.market_session(dt) == runner.PRE_MARKET


def test_market_session_just_before_open_is_pre_market():
    dt = datetime(2026, 7, 24, 9, 29, 59, tzinfo=ET)
    assert runner.market_session(dt) == runner.PRE_MARKET


def test_market_session_exactly_at_open_is_regular():
    dt = datetime(2026, 7, 24, 9, 30, 0, tzinfo=ET)  # boundary is `<`, so == is already REGULAR
    assert runner.market_session(dt) == runner.REGULAR


def test_market_session_just_before_close_is_regular():
    dt = datetime(2026, 7, 24, 15, 59, 59, tzinfo=ET)
    assert runner.market_session(dt) == runner.REGULAR


def test_market_session_exactly_at_close_is_after_hours():
    dt = datetime(2026, 7, 24, 16, 0, 0, tzinfo=ET)  # boundary is `<`, so == is already AFTER_HOURS
    assert runner.market_session(dt) == runner.AFTER_HOURS


def test_market_session_just_before_after_hours_window_ends():
    dt = datetime(2026, 7, 24, 19, 59, 59, tzinfo=ET)
    assert runner.market_session(dt) == runner.AFTER_HOURS


def test_market_session_exactly_at_after_hours_window_end_is_closed():
    dt = datetime(2026, 7, 24, 20, 0, 0, tzinfo=ET)  # boundary is `>=`, so == is already CLOSED
    assert runner.market_session(dt) == runner.CLOSED


def _fake_result(vix_passes):
    from signals.base import SubSignal
    from signals.buy_signal import BuySignal

    subs = [SubSignal("vix", 20.0, "none", "x", passes=vix_passes)]
    return BuySignal(score=0, state="none", passing_count=int(vix_passes), subsignals=subs)


def test_tick_returns_none_and_skips_compute_when_closed():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
    with patch("runner.compute_signal") as mock_compute:
        result = runner.tick(dt)
    assert result is None
    mock_compute.assert_not_called()


def test_tick_always_refreshes_in_pre_market():
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "runner.refresh_macro"
    ) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_always_refreshes_in_after_hours():
    dt = datetime(2026, 7, 24, 17, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "runner.refresh_macro"
    ) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_regular_refreshes_only_if_vix_passes():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=True)), patch(
        "runner.refresh_macro"
    ) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_regular_no_refresh_when_vix_not_passing():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "runner.refresh_macro"
    ) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_not_called()


def test_refresh_macro_gating():
    with patch("collectors.fed_rate.should_refresh", return_value=True), patch(
        "collectors.fed_rate.update_fed_rate_data"
    ) as mock_fed_update, patch("collectors.sectors.should_refresh", return_value=False), patch(
        "collectors.sectors.update_sector_data"
    ) as mock_sector_update, patch(
        "collectors.margin_debt.should_refresh", return_value=True
    ), patch(
        "runner._cf_bypass_ready", return_value=True
    ), patch(
        "collectors.margin_debt.update_margin_debt_data"
    ) as mock_margin_update:
        runner.refresh_macro()
    mock_fed_update.assert_called_once()
    mock_sector_update.assert_not_called()
    mock_margin_update.assert_called_once()


def test_refresh_macro_skips_margin_when_cfbypass_unreachable():
    with patch("collectors.fed_rate.should_refresh", return_value=False), patch(
        "collectors.sectors.should_refresh", return_value=False
    ), patch("collectors.margin_debt.should_refresh", return_value=True), patch(
        "runner._cf_bypass_ready", return_value=False
    ), patch(
        "collectors.margin_debt.update_margin_debt_data"
    ) as mock_margin_update:
        runner.refresh_macro()
    mock_margin_update.assert_not_called()


def test_run_forever_survives_a_failing_tick():
    calls = []

    def fake_tick():
        calls.append(1)
        if len(calls) >= 2:
            raise KeyboardInterrupt  # simulate Ctrl+C to end the loop deterministically
        raise RuntimeError("boom")

    with patch("runner.tick", side_effect=fake_tick), patch("runner.sleep"):
        runner.run_forever(interval_sec=0)
    assert len(calls) == 2


def test_run_forever_survives_a_failing_on_tick_callback():
    tick_calls = []

    def fake_tick():
        tick_calls.append(1)
        if len(tick_calls) >= 2:
            raise KeyboardInterrupt
        return "some_result"

    def bad_callback(result, error):
        raise ValueError("callback boom")

    with patch("runner.tick", side_effect=fake_tick), patch("runner.sleep"):
        runner.run_forever(interval_sec=0, on_tick=bad_callback)
    assert len(tick_calls) == 2


def test_run_forever_keyboard_interrupt_from_sleep_stops_cleanly(capsys):
    with patch("runner.tick", return_value=None), patch("runner.sleep", side_effect=KeyboardInterrupt):
        runner.run_forever(interval_sec=0)
    assert "Runner stopped." in capsys.readouterr().out


def test_cf_bypass_ready_true_when_reachable():
    with patch("runner.requests.get", return_value=None):
        assert runner._cf_bypass_ready(timeout=1.0) is True


def test_cf_bypass_ready_false_when_unreachable():
    import requests

    with patch("runner.requests.get", side_effect=requests.exceptions.ConnectionError()):
        assert runner._cf_bypass_ready(timeout=0.1) is False
