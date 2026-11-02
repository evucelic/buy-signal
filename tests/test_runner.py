"""Tests for schedule/runner.py: tick's refresh-trigger conditions, the per-collector
isolation of a refresh pass, and run_forever()'s failure resilience.

The session boundaries those triggers are read against live in test_market_hours.py.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from zoneinfo import ZoneInfo

from buysignal.schedule import runner
from buysignal.schedule.journal import RefreshJournal

ET = ZoneInfo("America/New_York")

# 2026-07-24 is a Friday (ordinary NYSE trading day, no holiday); 2026-07-25 is a Saturday.


def _fake_result(vix_passes):
    from buysignal.signals.base import SubSignal
    from buysignal.signals.buy_signal import BuySignal

    subs = [SubSignal("vix", 20.0, "none", "x", passes=vix_passes)]
    return BuySignal(score=0, state="none", passing_count=int(vix_passes), subsignals=subs)


def test_tick_returns_none_and_skips_compute_when_closed():
    dt = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal") as mock_compute:
        result = runner.tick(dt)
    assert result is None
    mock_compute.assert_not_called()


def test_tick_always_refreshes_in_pre_market():
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_refreshes_in_pre_market_even_when_the_vix_subsignal_is_missing():
    """A VIX collector failure used to mean current_vix is None and no macro refresh at all,
    pre-market included: the five macro caches then sat frozen until someone ran /refresh.
    """
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    from buysignal.signals.buy_signal import BuySignal

    vixless = BuySignal(score=0, state="none", passing_count=0, subsignals=[])
    with patch("buysignal.schedule.runner.compute_signal", return_value=vixless), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_reports_a_refresh_failure_instead_of_discarding_it(caplog):
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    with caplog.at_level(logging.INFO), patch(
        "buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)
    ), patch("buysignal.schedule.runner._any_due", return_value=False), patch(
        "buysignal.schedule.runner.refresh_macro", return_value=[("fed_rate", "SessionNotCreated")]
    ):
        runner.tick(dt)
    assert "fed_rate" in caplog.text
    assert "SessionNotCreated" in caplog.text


def test_tick_always_refreshes_in_after_hours():
    dt = datetime(2026, 7, 24, 17, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_regular_refreshes_only_if_vix_passes():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=True)), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_regular_no_refresh_when_vix_not_passing_and_nothing_is_due():
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro") as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_not_called()


def test_tick_regular_refreshes_whatever_the_journal_says_is_due_even_if_vix_not_passing():
    """A REGULAR session with a failing VIX must not black out margin_debt's monthly checkpoint
    (the bug that let a real FINRA release go uncaught until manually refreshed), nor a
    collector whose retry backoff has just elapsed.
    """
    dt = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "buysignal.schedule.runner._any_due", return_value=True
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


class _FakeCollector:
    """Stands in for one entry of runner's registry: a name, an update, and its outcome."""

    def __init__(self, name, demands=False, error=None, blocked_by=None, raises=None):
        self.name = name
        self.demands = demands
        self.error = error
        self.blocked_by = blocked_by
        self.raises = raises
        self.refreshed = False

    def demands_refresh(self):
        return self.demands

    def precondition(self):
        return self.blocked_by

    def update(self):
        self.refreshed = True
        if self.raises is not None:
            raise self.raises
        return self.error


NOW = datetime(2026, 7, 24, 5, 0, tzinfo=ET)


@pytest.fixture
def journal(tmp_path):
    return RefreshJournal(tmp_path / "buysignal.schedule.journal.json")


def _refresh(collectors, journal, force=False, now=NOW):
    with patch("buysignal.schedule.runner._macro_collectors", return_value=collectors):
        failed = runner.refresh_macro(force=force, journal=journal, now=now)
    return [c.name for c in collectors if c.refreshed], failed


def test_refresh_macro_refreshes_only_what_the_journal_says_is_due(journal):
    journal.record("sector", ok=True, error=None, now=NOW)
    collectors = [_FakeCollector("fed_rate"), _FakeCollector("sector")]

    refreshed, _ = _refresh(collectors, journal, now=NOW + timedelta(hours=2))

    assert refreshed == ["fed_rate"]


def test_refresh_macro_force_refreshes_collectors_that_are_not_due(journal):
    journal.record("fed_rate", ok=True, error=None, now=NOW)
    journal.record("sector", ok=True, error=None, now=NOW)
    collectors = [_FakeCollector("fed_rate"), _FakeCollector("sector")]

    refreshed, _ = _refresh(collectors, journal, force=True, now=NOW + timedelta(hours=2))

    assert refreshed == ["fed_rate", "sector"]


def test_refresh_macro_can_be_narrowed_to_named_collectors(journal):
    collectors = [_FakeCollector("fed_rate"), _FakeCollector("valuations")]

    with patch("buysignal.schedule.runner._macro_collectors", return_value=collectors):
        runner.refresh_macro(journal=journal, now=NOW, only=("valuations",))

    assert [c.name for c in collectors if c.refreshed] == ["valuations"]


def test_refresh_macro_reports_the_name_and_error_of_each_failure(journal):
    collectors = [
        _FakeCollector("fed_rate", error="ConnectionError: boom"),
        _FakeCollector("sector"),
        _FakeCollector("yield_curve", error="HTTPError: 503"),
    ]

    _, failed = _refresh(collectors, journal)

    assert failed == [("fed_rate", "ConnectionError: boom"), ("yield_curve", "HTTPError: 503")]


def test_refresh_macro_reports_nothing_when_every_collector_succeeds(journal):
    _, failed = _refresh([_FakeCollector("fed_rate"), _FakeCollector("sector")], journal)

    assert failed == []


def test_refresh_macro_skips_the_update_of_a_collector_whose_precondition_fails(journal):
    blocked = _FakeCollector("margin_debt", blocked_by="cfbypass service unreachable")
    collectors = [_FakeCollector("fed_rate"), blocked]

    refreshed, failed = _refresh(collectors, journal)

    assert refreshed == ["fed_rate"]
    assert failed == [("margin_debt", "cfbypass service unreachable")]


def test_refresh_macro_does_not_check_the_precondition_of_a_collector_that_is_not_due(journal):
    blocked = _FakeCollector("margin_debt", blocked_by="cfbypass service unreachable")
    journal.record("margin_debt", ok=True, error=None, now=NOW)

    _, failed = _refresh([blocked], journal, now=NOW + timedelta(hours=2))

    assert failed == []


def test_refresh_macro_keeps_going_when_a_collector_raises(journal):
    """One collector blowing up used to end the pass, and the two Selenium collectors ran
    first: a browser that wouldn't start cost every cache behind it its refresh.
    """
    collectors = [
        _FakeCollector("fed_rate", raises=RuntimeError("chrome not reachable")),
        _FakeCollector("sector"),
        _FakeCollector("valuations"),
    ]

    refreshed, _ = _refresh(collectors, journal)

    assert refreshed == ["fed_rate", "sector", "valuations"]


def test_refresh_macro_records_a_raised_exception_as_that_collectors_error(journal):
    collectors = [_FakeCollector("fed_rate", raises=RuntimeError("chrome not reachable"))]

    _, failed = _refresh(collectors, journal)

    assert failed == [("fed_rate", "RuntimeError: chrome not reachable")]
    assert journal.unreported_failures(NOW) == [("fed_rate", "RuntimeError: chrome not reachable")]


def test_refresh_macro_records_every_attempt_it_makes(journal):
    collectors = [_FakeCollector("fed_rate"), _FakeCollector("sector", error="HTTPError: 503")]

    _refresh(collectors, journal)

    statuses = {s.name: s for s in journal.statuses()}
    assert statuses["fed_rate"].ok is True
    assert statuses["sector"].ok is False
    assert statuses["sector"].attempted_at == NOW


def test_refresh_macro_does_not_record_a_collector_it_skipped(journal):
    journal.record("sector", ok=True, error=None, now=NOW)

    _refresh([_FakeCollector("fed_rate")], journal, now=NOW + timedelta(hours=2))

    assert [s.name for s in journal.statuses()] == ["sector", "fed_rate"]


def test_cf_bypass_precondition_passes_when_the_service_is_reachable():
    with patch("buysignal.schedule.runner._cf_bypass_ready", return_value=True):
        assert runner._cf_bypass_precondition() is None


def test_cf_bypass_precondition_reports_the_url_when_the_service_is_unreachable():
    with patch("buysignal.schedule.runner._cf_bypass_ready", return_value=False):
        error = runner._cf_bypass_precondition()

    assert "cfbypass" in error
    assert runner.CF_BYPASS_URL in error


def test_macro_collectors_covers_every_macro_cache_cheapest_first():
    """Order is the cheap failure containment: the two Selenium collectors run last, so a
    browser that won't start can't cost the three that don't need one.
    """
    assert [c.name for c in runner._macro_collectors()] == [
        "yield_curve",
        "sector",
        "margin_debt",
        "fed_rate",
        "valuations",
    ]


def test_run_forever_survives_a_failing_tick():
    calls = []

    def fake_tick():
        calls.append(1)
        if len(calls) >= 2:
            raise KeyboardInterrupt  # simulate Ctrl+C to end the loop deterministically
        raise RuntimeError("boom")

    with patch("buysignal.schedule.runner.tick", side_effect=fake_tick), patch("buysignal.schedule.runner.sleep"):
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

    with patch("buysignal.schedule.runner.tick", side_effect=fake_tick), patch("buysignal.schedule.runner.sleep"):
        runner.run_forever(interval_sec=0, on_tick=bad_callback)
    assert len(tick_calls) == 2


def test_run_forever_keyboard_interrupt_from_sleep_stops_cleanly(caplog):
    with caplog.at_level(logging.INFO), patch("buysignal.schedule.runner.tick", return_value=None), patch(
        "buysignal.schedule.runner.sleep", side_effect=KeyboardInterrupt
    ):
        runner.run_forever(interval_sec=0)
    assert "Runner stopped." in caplog.text


def test_cf_bypass_ready_true_when_reachable():
    with patch("buysignal.schedule.runner.requests.get", return_value=None):
        assert runner._cf_bypass_ready(timeout=1.0) is True


def test_cf_bypass_ready_false_when_unreachable():
    import requests

    with patch("buysignal.schedule.runner.requests.get", side_effect=requests.exceptions.ConnectionError()):
        assert runner._cf_bypass_ready(timeout=0.1) is False


# --- tick(): CLOSED-but-vix-active behavior ---------------------------------------------------------


def test_tick_runs_when_closed_but_vix_window_active():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    assert runner.market_session(dt) == runner.CLOSED
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)) as mock_compute, patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro") as mock_refresh:
        result = runner.tick(dt)
    assert result is not None
    mock_compute.assert_called_once()
    mock_refresh.assert_not_called()  # closed, and this vix reading doesn't pass


def test_tick_idle_when_closed_and_vix_window_inactive():
    dt = datetime(2026, 7, 24, 21, 0, tzinfo=ET)  # 9pm ET = 8pm CT: both NYSE closed and VIX quiet
    assert runner.market_session(dt) == runner.CLOSED
    with patch("buysignal.schedule.runner.compute_signal") as mock_compute:
        result = runner.tick(dt)
    assert result is None
    mock_compute.assert_not_called()


def test_tick_refreshes_during_closed_vix_window_if_vix_passes():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=True)), patch(
        "buysignal.schedule.runner._any_due", return_value=False
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
        runner.tick(dt)
    mock_refresh.assert_called_once()


def test_tick_refreshes_during_closed_vix_window_when_a_collector_is_due():
    dt = datetime(2026, 7, 24, 3, 0, tzinfo=ET)  # 3am ET = 2am CT: NYSE closed, VIX active
    with patch("buysignal.schedule.runner.compute_signal", return_value=_fake_result(vix_passes=False)), patch(
        "buysignal.schedule.runner._any_due", return_value=True
    ), patch("buysignal.schedule.runner.refresh_macro", return_value=[]) as mock_refresh:
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

    with patch("buysignal.schedule.runner.tick", return_value=None), patch("buysignal.schedule.runner.sleep", side_effect=fake_sleep):
        runner.run_forever(interval_sec=3600)
    assert calls == [3600 - (1_700_003_600.0 % 3600)]


def test_any_due_survives_a_domain_rule_that_raises():
    """A refresh gate blowing up costs that collector an opinion, not the caller its tick."""
    exploding = _FakeCollector("margin_debt")
    exploding.demands_refresh = lambda: 1 / 0

    with patch("buysignal.schedule.runner._macro_collectors", return_value=[exploding]):
        assert runner._any_due(NOW) is True


def test_tick_returns_the_post_refresh_assessment():
    dt = datetime(2026, 7, 24, 5, 0, tzinfo=ET)
    before = _fake_result(vix_passes=False)
    after = _fake_result(vix_passes=True)
    with patch.object(runner, "compute_signal", side_effect=[before, after]) as compute, \
         patch.object(runner, "refresh_macro", return_value=[]), \
         patch.object(runner, "report") as report:
        result = runner.tick(dt)
    assert result is after
    assert compute.call_count == 2
    assert compute.call_args.kwargs == {"allow_refresh": False}
    report.assert_called_once_with(after, runner.PRE_MARKET, dt)
