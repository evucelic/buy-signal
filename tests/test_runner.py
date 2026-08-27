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
    """A REGULAR session with a failing VIX must not black out margin_debt's monthly checkpoint:
    this is the bug that let a real FINRA release go uncaught until manually refreshed.
    """
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "collectors.margin_debt.should_refresh", return_value=True
    ), patch("runner.refresh_macro") as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


class _FakeCollector:
    def __init__(self, name, stale=True, error=None, blocked_by=None):
        self.name = name
        self.stale = stale
        self.error = error
        self.blocked_by = blocked_by
        self.refreshed = False

    def should_refresh(self):
        return self.stale

    def precondition(self):
        return self.blocked_by

    def update(self):
        self.refreshed = True
        return self.error


def _refresh(collectors, force=False):
    with patch("runner._macro_collectors", return_value=collectors):
        failed = runner.refresh_macro(force=force)
    return [c.name for c in collectors if c.refreshed], failed


def test_refresh_macro_refreshes_only_the_stale_collectors():
    collectors = [_FakeCollector("fed_rate", stale=True), _FakeCollector("sector", stale=False)]

    refreshed, _ = _refresh(collectors)

    assert refreshed == ["fed_rate"]


def test_refresh_macro_force_refreshes_collectors_that_are_not_stale():
    collectors = [_FakeCollector("fed_rate", stale=False), _FakeCollector("sector", stale=False)]

    refreshed, _ = _refresh(collectors, force=True)

    assert refreshed == ["fed_rate", "sector"]


def test_refresh_macro_reports_the_name_and_error_of_each_failure():
    collectors = [
        _FakeCollector("fed_rate", error="ConnectionError: boom"),
        _FakeCollector("sector"),
        _FakeCollector("yield_curve", error="HTTPError: 503"),
    ]

    _, failed = _refresh(collectors)

    assert failed == [("fed_rate", "ConnectionError: boom"), ("yield_curve", "HTTPError: 503")]


def test_refresh_macro_reports_nothing_when_every_collector_succeeds():
    _, failed = _refresh([_FakeCollector("fed_rate"), _FakeCollector("sector")])

    assert failed == []


def test_refresh_macro_skips_the_update_of_a_collector_whose_precondition_fails():
    blocked = _FakeCollector("margin_debt", blocked_by="cfbypass service unreachable")
    collectors = [_FakeCollector("fed_rate"), blocked]

    refreshed, failed = _refresh(collectors)

    assert refreshed == ["fed_rate"]
    assert failed == [("margin_debt", "cfbypass service unreachable")]


def test_refresh_macro_does_not_check_the_precondition_of_a_fresh_collector():
    blocked = _FakeCollector("margin_debt", stale=False, blocked_by="cfbypass service unreachable")

    _, failed = _refresh([blocked])

    assert failed == []


def test_cf_bypass_precondition_passes_when_the_service_is_reachable():
    with patch("runner._cf_bypass_ready", return_value=True):
        assert runner._cf_bypass_precondition() is None


def test_cf_bypass_precondition_reports_the_url_when_the_service_is_unreachable():
    with patch("runner._cf_bypass_ready", return_value=False):
        error = runner._cf_bypass_precondition()

    assert "cfbypass" in error
    assert runner.CF_BYPASS_URL in error


def test_macro_collectors_covers_every_macro_cache():
    assert [c.name for c in runner._macro_collectors()] == [
        "fed_rate",
        "sector",
        "yield_curve",
        "valuations",
        "margin_debt",
    ]


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
