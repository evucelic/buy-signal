"""Tests for schedule/journal.py: the retry/daily gating, the once-a-day failure dedup, and
the JSON persistence behind them.

Every clock is a literal passed in, and every journal is built on tmp_path, so nothing here
monkeypatches time or touches the real data directory.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from buysignal.schedule.journal import RefreshJournal

ET = ZoneInfo("America/New_York")

MORNING = datetime(2026, 9, 14, 5, 0, tzinfo=ET)
NOON = datetime(2026, 9, 14, 12, 0, tzinfo=ET)
NEXT_DAY = datetime(2026, 9, 15, 5, 0, tzinfo=ET)

BACKOFF = timedelta(hours=2)


class _FakeCollector:
    """The Refreshable contract: a name, and a domain rule that can demand a refresh."""

    def __init__(self, name="fed_rate", demands=False):
        self.name = name
        self._demands = demands

    def demands_refresh(self):
        return self._demands


def _journal(tmp_path, backoff=BACKOFF):
    return RefreshJournal(tmp_path / "buysignal.schedule.journal.json", backoff=backoff)


# --- due(): the single timing authority ---------------------------------------------------


def test_due_when_never_attempted(tmp_path):
    assert _journal(tmp_path).due(_FakeCollector(), MORNING) is True


def test_not_due_after_a_success_the_same_day(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=True, error=None, now=MORNING)

    assert journal.due(_FakeCollector(), NOON) is False


def test_due_again_the_next_day(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=True, error=None, now=MORNING)

    assert journal.due(_FakeCollector(), NEXT_DAY) is True


def test_not_due_inside_the_backoff_window_after_a_failure(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="boom", now=MORNING)

    assert journal.due(_FakeCollector(), MORNING + timedelta(minutes=59)) is False


def test_due_again_once_the_backoff_window_elapses(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="boom", now=MORNING)

    assert journal.due(_FakeCollector(), MORNING + BACKOFF) is True


def test_a_domain_demand_overrides_the_daily_gate(tmp_path):
    """margin_debt's FINRA checkpoint: a success today doesn't mean there's nothing new to find."""
    journal = _journal(tmp_path)
    journal.record("margin_debt", ok=True, error=None, now=MORNING)

    assert journal.due(_FakeCollector("margin_debt", demands=True), NOON) is True


def test_a_domain_demand_does_not_override_the_backoff(tmp_path):
    """Backoff is about the collector being broken right now; no domain rule argues with that."""
    journal = _journal(tmp_path)
    journal.record("margin_debt", ok=False, error="cfbypass unreachable", now=MORNING)

    assert journal.due(_FakeCollector("margin_debt", demands=True), MORNING + timedelta(minutes=30)) is False


# --- unreported_failures() / mark_reported(): the dedup is the point -----------------------


def test_a_failure_is_reported_once_and_then_not_again(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="SessionNotCreated", now=MORNING)

    assert journal.unreported_failures(MORNING) == [("fed_rate", "SessionNotCreated")]
    journal.mark_reported("fed_rate", MORNING)
    assert journal.unreported_failures(NOON) == []


def test_a_failure_stays_pending_until_it_is_marked_reported(tmp_path):
    """Asking is not delivering: if the push never goes out, the next tick must try again."""
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="SessionNotCreated", now=MORNING)

    journal.unreported_failures(MORNING)

    assert journal.unreported_failures(NOON) == [("fed_rate", "SessionNotCreated")]


def test_marking_an_unknown_collector_is_harmless(tmp_path):
    _journal(tmp_path).mark_reported("never_heard_of_it", MORNING)


def test_a_failure_that_persists_is_reported_again_the_next_day(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="SessionNotCreated", now=MORNING)
    journal.mark_reported("fed_rate", MORNING)
    journal.record("fed_rate", ok=False, error="SessionNotCreated", now=NEXT_DAY)

    assert journal.unreported_failures(NEXT_DAY) == [("fed_rate", "SessionNotCreated")]


def test_a_success_is_never_reported(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=True, error=None, now=MORNING)

    assert journal.unreported_failures(MORNING) == []


def test_a_collector_that_recovers_stops_being_reported(tmp_path):
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="boom", now=MORNING)
    journal.record("fed_rate", ok=True, error=None, now=NEXT_DAY)

    assert journal.unreported_failures(NEXT_DAY) == []


# --- statuses(): what /status renders ------------------------------------------------------


def test_statuses_carry_the_attempt_its_result_and_its_error(tmp_path):
    journal = _journal(tmp_path)
    journal.record("yield_curve", ok=True, error=None, now=MORNING)
    journal.record("margin_debt", ok=False, error="cfbypass unreachable", now=NOON)

    statuses = {s.name: s for s in journal.statuses()}

    assert statuses["yield_curve"].ok is True
    assert statuses["yield_curve"].error is None
    assert statuses["yield_curve"].attempted_at == MORNING
    assert statuses["margin_debt"].ok is False
    assert statuses["margin_debt"].error == "cfbypass unreachable"


def test_statuses_are_empty_before_anything_is_attempted(tmp_path):
    assert _journal(tmp_path).statuses() == []


# --- persistence ---------------------------------------------------------------------------


def test_state_survives_a_reopen(tmp_path):
    _journal(tmp_path).record("fed_rate", ok=True, error=None, now=MORNING)

    assert _journal(tmp_path).due(_FakeCollector(), NOON) is False


def test_a_reported_failure_stays_reported_across_a_reopen(tmp_path):
    """A redeploy must not re-push the failure you were already told about."""
    journal = _journal(tmp_path)
    journal.record("fed_rate", ok=False, error="boom", now=MORNING)
    journal.mark_reported("fed_rate", MORNING)

    assert _journal(tmp_path).unreported_failures(NOON) == []


def test_a_corrupt_file_degrades_to_refreshing_everything(tmp_path):
    path = tmp_path / "buysignal.schedule.journal.json"
    path.write_text("{not json at all")

    journal = RefreshJournal(path, backoff=BACKOFF)

    assert journal.statuses() == []
    assert journal.due(_FakeCollector(), MORNING) is True


def test_an_unparseable_entry_is_dropped_without_losing_the_others(tmp_path):
    path = tmp_path / "buysignal.schedule.journal.json"
    path.write_text('{"fed_rate": {"attempted_at": "not-a-date"}, "sector": '
                    '{"attempted_at": "2026-09-14T05:00:00-04:00", "ok": true, "error": null}}')

    journal = RefreshJournal(path, backoff=BACKOFF)

    assert [s.name for s in journal.statuses()] == ["sector"]


def test_a_missing_data_directory_is_created_on_the_first_write(tmp_path):
    path = tmp_path / "nested" / "buysignal.schedule.journal.json"
    RefreshJournal(path, backoff=BACKOFF).record("fed_rate", ok=True, error=None, now=MORNING)

    assert path.exists()
