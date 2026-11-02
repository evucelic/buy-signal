"""FedWatch snapshot publication and refresh gates; no network or live browser."""

from unittest.mock import patch

import pandas as pd
import pytest

from buysignal.collectors import fed_rate
from buysignal.schedule import runner

TODAY = pd.Timestamp("2026-10-29")


@pytest.fixture
def snapshot():
    return pd.DataFrame([
        {"horizon": horizon, "meeting_date": pd.Timestamp(date),
         "prob_ease": 0.0, "prob_no_change": 0.9, "prob_hike": 0.1}
        for horizon, date in (
            ("nearest", "2026-12-09"),
            ("six_month", "2027-04-28"),
            ("one_year", "2027-10-27"),
        )
    ])


@pytest.mark.parametrize("problem", ["empty", "missing_column", "missing_horizon", "duplicate_horizon",
                                      "missing_date", "past_date", "reversed_dates", "bad_probability"])
def test_invalid_snapshot_keeps_previous_cache(tmp_path, snapshot, problem):
    if problem == "empty":
        snapshot = snapshot.iloc[:0]
    elif problem == "missing_column":
        snapshot = snapshot.drop(columns="prob_hike")
    elif problem == "missing_horizon":
        snapshot = snapshot.iloc[:2]
    elif problem == "duplicate_horizon":
        snapshot.loc[2, "horizon"] = "six_month"
    elif problem == "missing_date":
        snapshot.loc[0, "meeting_date"] = pd.NaT
    elif problem == "past_date":
        snapshot.loc[0, "meeting_date"] = TODAY - pd.Timedelta(days=1)
    elif problem == "reversed_dates":
        snapshot.loc[0, "meeting_date"] = pd.Timestamp("2027-12-01")
    elif problem == "bad_probability":
        snapshot.loc[0, "prob_hike"] = float("nan")

    path = tmp_path / "fed.csv"
    path.write_text("last good snapshot")
    original_mtime = path.stat().st_mtime_ns
    with patch.object(fed_rate, "_today", return_value=TODAY), \
         patch.object(fed_rate.time, "sleep"), \
         patch.object(fed_rate, "_fetch_with_retries", return_value=(snapshot, None)):
        error = fed_rate.update_fed_rate_data(path)

    assert error.startswith("Invalid FedWatch snapshot:")
    assert path.read_text() == "last good snapshot"
    assert path.stat().st_mtime_ns == original_mtime
    assert list(tmp_path.iterdir()) == [path]


def test_valid_snapshot_is_published_and_loadable(tmp_path, snapshot):
    path = tmp_path / "fed.csv"
    path.write_text("old cache")
    with patch.object(fed_rate, "_today", return_value=TODAY), \
         patch.object(fed_rate.time, "sleep"), \
         patch.object(fed_rate, "_fetch_with_retries", return_value=(snapshot, None)):
        assert fed_rate.update_fed_rate_data(path) is None
        loaded = fed_rate.latest_fedwatch(path)
    pd.testing.assert_frame_equal(loaded, snapshot)
    assert list(tmp_path.iterdir()) == [path]


def test_failed_write_preserves_previous_snapshot_and_cleans_temporary_file(tmp_path, snapshot):
    path = tmp_path / "fed.csv"
    path.write_text("old cache")
    with patch.object(fed_rate, "_today", return_value=TODAY), \
         patch.object(fed_rate.time, "sleep"), \
         patch.object(fed_rate, "_fetch_with_retries", return_value=(snapshot, None)), \
         patch.object(pd.DataFrame, "to_csv", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            fed_rate.update_fed_rate_data(path)
    assert path.read_text() == "old cache"
    assert list(tmp_path.iterdir()) == [path]


def test_fresh_file_with_a_past_meeting_is_not_usable(tmp_path, snapshot):
    path = tmp_path / "fed.csv"
    snapshot.to_csv(path, index=False)
    with patch.object(fed_rate, "_today", return_value=pd.Timestamp("2026-12-10")):
        with pytest.raises(ValueError, match="past meeting"):
            fed_rate.latest_fedwatch(path)


def test_invalid_fetch_is_retried_before_publication(snapshot):
    invalid = snapshot.iloc[:0]
    with patch.object(fed_rate, "_fetch_meeting_probabilities", side_effect=[invalid, snapshot]) as fetch, \
         patch.object(fed_rate.time, "sleep"):
        result, error = fed_rate._fetch_with_retries(TODAY)
    assert error is None
    assert fetch.call_count == 2
    pd.testing.assert_frame_equal(result, snapshot)


@pytest.mark.parametrize("error", [FileNotFoundError(), ValueError("past meeting"), ValueError("older than 48h")])
def test_missing_or_invalid_cache_demands_refresh(error):
    with patch.object(fed_rate, "latest_fedwatch", side_effect=error):
        assert fed_rate.demands_refresh() is True


def test_valid_cache_does_not_demand_extra_refresh(snapshot):
    with patch.object(fed_rate, "latest_fedwatch", return_value=snapshot):
        assert fed_rate.demands_refresh() is False


def test_runner_registers_fedwatch_rollover_refresh_gate():
    collector = next(c for c in runner._macro_collectors() if c.name == "fed_rate")
    assert collector.demands_refresh is fed_rate.demands_refresh
