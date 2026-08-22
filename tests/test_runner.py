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
        "collectors.margin_debt.should_refresh", return_value=False
    ), patch("runner.refresh_macro") as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_not_called()


def test_tick_regular_refreshes_when_margin_debt_checkpoint_due_even_if_vix_not_passing():
    """A REGULAR session with a failing VIX must not black out margin_debt's monthly checkpoint —
    this is the bug that let a real FINRA release go uncaught until manually refreshed.
    """
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "collectors.margin_debt.should_refresh", return_value=True
    ), patch("runner.refresh_macro") as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


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
    ) as mock_margin_update, patch(
        "collectors.yield_curve.should_refresh", return_value=True
    ), patch(
        "collectors.yield_curve.update_yield_curve_data"
    ) as mock_curve_update:
        runner.refresh_macro()
    mock_fed_update.assert_called_once()
    mock_sector_update.assert_not_called()
    mock_margin_update.assert_called_once()
    mock_curve_update.assert_called_once()


def test_refresh_macro_skips_margin_when_cfbypass_unreachable():
    with patch("collectors.fed_rate.should_refresh", return_value=False), patch(
        "collectors.sectors.should_refresh", return_value=False
    ), patch("collectors.yield_curve.should_refresh", return_value=False), patch(
        "collectors.margin_debt.should_refresh", return_value=True
    ), patch(
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


# --- vix_active_window ---------------------------------------------------------


def test_vix_active_window_just_before_start_is_inactive():
    dt = datetime(2026, 7, 24, 1, 59, tzinfo=runner.CT)
    assert runner.vix_active_window(dt) is False


def test_vix_active_window_exactly_at_start_is_active():
    dt = datetime(2026, 7, 24, 2, 0, tzinfo=runner.CT)  # boundary is `<=`, so == is already active
    assert runner.vix_active_window(dt) is True


def test_vix_active_window_just_before_end_is_active():
    dt = datetime(2026, 7, 24, 19, 59, tzinfo=runner.CT)
    assert runner.vix_active_window(dt) is True


def test_vix_active_window_exactly_at_end_is_inactive():
    dt = datetime(2026, 7, 24, 20, 0, tzinfo=runner.CT)  # boundary is `<`, so == is already inactive
    assert runner.vix_active_window(dt) is False


def test_vix_active_window_inactive_on_weekend_even_within_hour_range():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=runner.CT)  # Saturday, well within 02:00-20:00
    assert runner.vix_active_window(dt) is False


# --- tick(): CLOSED-but-vix-active behavior ---------------------------------------------------------


def test_tick_runs_when_closed_but_vix_window_active():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    assert runner.market_session(dt) == runner.CLOSED
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)) as mock_compute, patch(
        "collectors.margin_debt.should_refresh", return_value=False
    ), patch("runner.refresh_macro") as mock_refresh:
        result = runner.tick(dt)
    assert result is not None
    mock_compute.assert_called_once()
    mock_refresh.assert_not_called()  # closed, and this vix reading doesn't pass


def test_tick_idle_when_closed_and_vix_window_inactive():
    dt = datetime(2026, 7, 24, 21, 0, tzinfo=ET)  # 9pm ET = 8pm CT: both NYSE closed and VIX quiet
    assert runner.market_session(dt) == runner.CLOSED
    with patch("runner.compute_signal") as mock_compute:
        result = runner.tick(dt)
    assert result is None
    mock_compute.assert_not_called()


def test_tick_refreshes_during_closed_vix_window_if_vix_passes():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=True)), patch(
        "runner.refresh_macro"
    ) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_refreshes_during_closed_vix_window_when_margin_debt_checkpoint_due():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "collectors.margin_debt.should_refresh", return_value=True
    ), patch("runner.refresh_macro") as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


# --- refresh_macro(force=True) ---------------------------------------------------------


def test_refresh_macro_force_bypasses_should_refresh_gates():
    with patch("collectors.fed_rate.should_refresh", return_value=False), patch(
        "collectors.fed_rate.update_fed_rate_data"
    ) as mock_fed_update, patch("collectors.sectors.should_refresh", return_value=False), patch(
        "collectors.sectors.update_sector_data"
    ) as mock_sector_update, patch(
        "collectors.margin_debt.should_refresh", return_value=False
    ), patch(
        "runner._cf_bypass_ready", return_value=True
    ), patch(
        "collectors.margin_debt.update_margin_debt_data"
    ) as mock_margin_update, patch(
        "collectors.yield_curve.should_refresh", return_value=False
    ), patch(
        "collectors.yield_curve.update_yield_curve_data"
    ) as mock_curve_update:
        runner.refresh_macro(force=True)
    mock_fed_update.assert_called_once()
    mock_sector_update.assert_called_once()
    mock_margin_update.assert_called_once()
    mock_curve_update.assert_called_once()


def test_refresh_macro_returns_name_and_error_for_each_failed_collector():
    with patch("collectors.fed_rate.update_fed_rate_data", return_value="ConnectionError: boom"), patch(
        "collectors.sectors.update_sector_data", return_value=None
    ), patch("runner._cf_bypass_ready", return_value=True), patch(
        "collectors.margin_debt.update_margin_debt_data", return_value="TimeoutError: slow"
    ), patch(
        "collectors.yield_curve.update_yield_curve_data", return_value="HTTPError: 503"
    ):
        failed = runner.refresh_macro(force=True)
    assert ("fed_rate", "ConnectionError: boom") in failed
    assert ("margin_debt", "TimeoutError: slow") in failed
    assert ("yield_curve", "HTTPError: 503") in failed
    assert not any(name == "sector" for name, _ in failed)


def test_refresh_macro_reports_margin_debt_when_cfbypass_unreachable():
    with patch("collectors.fed_rate.update_fed_rate_data", return_value=None), patch(
        "collectors.sectors.update_sector_data", return_value=None
    ), patch("collectors.yield_curve.update_yield_curve_data", return_value=None), patch(
        "runner._cf_bypass_ready", return_value=False
    ):
        failed = runner.refresh_macro(force=True)
    assert len(failed) == 1
    name, error = failed[0]
    assert name == "margin_debt"
    assert "cfbypass" in error


# --- run_forever(): hour-aligned sleep ---------------------------------------------------------


def test_seconds_until_next_boundary_passes_through_non_positive_interval():
    assert runner._seconds_until_next_boundary(0) == 0


def test_seconds_until_next_boundary_aligns_to_wall_clock_hour(monkeypatch):
    fixed_time = 1_700_000_000.0
    remainder = fixed_time % 3600
    monkeypatch.setattr(runner, "wall_time", lambda: fixed_time)
    assert runner._seconds_until_next_boundary(3600) == 3600 - remainder


def test_run_forever_sleeps_until_next_hour_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "wall_time", lambda: 1_700_003_600.0)

    def fake_sleep(sec):
        calls.append(sec)
        raise KeyboardInterrupt

    with patch("runner.tick", return_value=None), patch("runner.sleep", side_effect=fake_sleep):
        runner.run_forever(interval_sec=3600)
    assert calls == [3600 - (1_700_003_600.0 % 3600)]
