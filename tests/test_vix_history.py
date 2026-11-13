"""Decades of daily VIX closes give the fear reading a scale without touching the fear rule."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import vix_history
from buysignal.signals import vix_signal


def closes(values, end="2026-10-30"):
    return pd.DataFrame({"date": pd.bdate_range(end=end, periods=len(values)), "vix": values})


def test_download_extends_the_archive_and_keeps_older_local_rows(tmp_path):
    path = tmp_path / "vixhistory.csv"
    closes([15.] * 40).to_csv(path, index=False)
    new = closes([15.] * 4 + [31.]).rename(columns={"vix": "value"})
    with patch.object(vix_history, "fetch_series_with_retries", return_value=(new, None)):
        assert vix_history.update_vix_history(path) is None
    loaded = vix_history.vix_long_history(path)
    assert len(loaded) == 40 and loaded.vix.iloc[-1] == 31.


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0., -3., 900.])
def test_invalid_download_keeps_the_last_good_archive(tmp_path, value):
    path = tmp_path / "vixhistory.csv"
    path.write_text("last good cache")
    new = closes([value]).rename(columns={"vix": "value"})
    with patch.object(vix_history, "fetch_series_with_retries", return_value=(new, None)):
        assert "Invalid VIX history" in vix_history.update_vix_history(path)
    assert path.read_text() == "last good cache"


def test_future_or_shorter_download_is_rejected(tmp_path):
    path = tmp_path / "vixhistory.csv"
    closes([15.] * 10).to_csv(path, index=False)
    original = path.read_text()
    future = closes([15.], end="2199-01-04").rename(columns={"vix": "value"})
    earlier = closes([15.] * 3, end="2026-10-01").rename(columns={"vix": "value"})
    for frame, reason in ((future, "future"), (earlier, "ends before")):
        with patch.object(vix_history, "fetch_series_with_retries", return_value=(frame, None)):
            assert reason in vix_history.update_vix_history(path)
        assert path.read_text() == original


def test_failed_download_reports_the_error_without_writing(tmp_path):
    path = tmp_path / "vixhistory.csv"
    with patch.object(vix_history, "fetch_series_with_retries", return_value=(None, "HTTPError: 503")):
        assert vix_history.update_vix_history(path) == "HTTPError: 503"
    assert not path.exists()


def test_recent_hourly_rows_win_and_missing_archive_is_not_an_error(tmp_path):
    recent = closes([20.] * 30)
    recent["high"] = 21.
    recent.attrs["observed_at"] = "2026-10-30T20:00:00+00:00"
    assert vix_history.with_long_history(recent, tmp_path / "absent.csv") is recent
    path = tmp_path / "vixhistory.csv"
    closes([12.] * 300).to_csv(path, index=False)  # overlaps the recent rows with other values
    combined = vix_history.with_long_history(recent, path)
    assert len(combined) == 300 and (combined.vix.tail(30) == 20.).all() and (combined.vix.head(270) == 12.).all()
    assert combined.attrs["observed_at"] == "2026-10-30T20:00:00+00:00"
    path.write_text("date,vix\nnot-a-date,x\n")
    assert vix_history.with_long_history(recent, path) is recent


def test_long_history_scales_the_reading_but_never_changes_the_rule():
    recent = closes([20.] * 24 + [25.])
    recent["high"] = recent.vix
    older = closes(list(np.linspace(10, 60, 2000)), end="2026-09-01")
    def score(history):
        with patch.object(vix_signal, "daily_history", return_value=recent), \
             patch.object(vix_signal, "vix_change_pct", return_value=.1), \
             patch.object(vix_signal, "with_long_history", return_value=history):
            return vix_signal.score(vix=25.)
    short, long = score(recent), score(pd.concat([older, recent], ignore_index=True))
    assert (short.passes, short.state, short.metrics["recent_peak"]) == (long.passes, long.state, long.metrics["recent_peak"])
    assert short.metrics["percentile"] is None  # 25 sessions cannot say how unusual a reading is
    assert long.metrics["since"] == str(older.date.iloc[0].date())
    assert long.metrics["percentile"] == pytest.approx(float((pd.concat([older.vix, recent.vix]) <= 25).mean()))
    assert len(long.history) == 2025
