"""Entry geometry, causal volatility baselines and daily-history freshness."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.collectors import entry
from buysignal.signals import market_signal, trend_signal


def prices(values):
    values = np.array(values, dtype=float)
    return pd.DataFrame({name: values.copy() for name in ("SPY", "NASDAQ", "DOW")},
                        index=pd.bdate_range("2024-01-02", periods=len(values), name="date"))


def score(values):
    with patch.object(market_signal, "get_entry_history", return_value=prices(values)):
        return market_signal.score()


def test_drawdown_remains_active_on_a_rebound_day():
    signal = score([100.] * 218 + [92., 93.])
    assert signal.passes
    assert signal.score == pytest.approx(-.07)
    assert signal.metrics["daily"] > 0


def test_a_red_day_does_not_qualify_without_a_five_percent_drawdown():
    signal = score([100.] * 219 + [99.])
    assert not signal.passes
    assert signal.score == pytest.approx(-.01)


def test_new_high_is_zero_drawdown():
    assert score([100.] * 219 + [102.]).score == 0


def test_last_occurrence_of_a_tied_high_is_the_peak_date():
    frame = prices([100.] * 219 + [94.])
    facts = market_signal.analyze_history(frame)
    assert facts["peak_date"] == str(frame.index[-2].date())


def test_peak_outside_the_lookback_is_excluded():
    signal = score([200.] * 150 + [100.] * 69 + [96.])
    assert not signal.passes
    assert signal.metrics["peak"] == 100


def test_drawdown_has_no_silent_short_history_fallback():
    with pytest.raises(ValueError, match="64 sessions"):
        score([100.] * 63)


def test_five_session_intensity_uses_volatility_before_the_move():
    baseline_returns = np.array([.01, -.01] * 10)
    baseline_prices = 100 * np.exp(np.r_[0, baseline_returns].cumsum())
    move = baseline_prices[-1] * np.exp(np.arange(1, 6) * -.02)
    values = np.r_[[100.] * 80, baseline_prices, move]
    facts = market_signal.analyze_history(prices(values))
    volatility = baseline_returns.std(ddof=1)
    assert facts["daily_volatility"] == pytest.approx(volatility)
    assert facts["intensity"] == pytest.approx(-.1 / (volatility * np.sqrt(5)))
    larger_move = baseline_prices[-1] * np.exp(np.arange(1, 6) * -.04)
    larger = market_signal.analyze_history(prices(np.r_[[100.] * 80, baseline_prices, larger_move]))
    assert larger["daily_volatility"] == pytest.approx(volatility)
    assert larger["intensity"] == pytest.approx(2 * facts["intensity"])


def test_zero_baseline_volatility_is_unknown_not_infinite():
    signal = score([100.] * 219 + [90.])
    assert signal.metrics["intensity"] is None
    assert signal.passes


@pytest.mark.parametrize("values, state", [
    (np.linspace(80, 120, 240), "uptrend"),
    (np.linspace(120, 80, 240), "downtrend"),
    (np.r_[np.linspace(80, 120, 239), 90], "under_pressure"),
    (np.r_[np.linspace(120, 80, 239), 115], "recovering"),
])
def test_trend_classifications_are_advisory(values, state):
    trend = trend_signal.from_market(score(values))
    assert trend.state == state
    assert trend.advisory


def test_short_trend_history_does_not_remove_valid_drawdown():
    signal = score([100.] * 63 + [94.])
    assert signal.passes
    assert trend_signal.from_market(signal).state == "unknown"


def test_cached_daily_data_is_valid_over_a_weekend():
    frame = prices([100.] * 100)
    frame.index = pd.bdate_range(end="2026-07-24", periods=100)
    entry._validate_recency(frame, pd.Timestamp("2026-07-26 12:00Z"))


def test_old_session_is_rejected_when_trading_has_started():
    frame = prices([100.] * 100)
    frame.index = pd.bdate_range(end="2026-07-24", periods=100)
    with pytest.raises(ValueError, match="expected session 2026-07-27"):
        entry._validate_recency(frame, pd.Timestamp("2026-07-27 15:00Z"))


def test_previous_session_is_valid_before_open():
    frame = prices([100.] * 100)
    frame.index = pd.bdate_range(end="2026-07-24", periods=100)
    entry._validate_recency(frame, pd.Timestamp("2026-07-27 12:00Z"))


def test_failed_download_does_not_replace_adjusted_history(tmp_path):
    path = tmp_path / "daily.csv"
    path.write_text("last good cache")
    with patch.object(entry.yf, "download", return_value=pd.DataFrame()), patch.object(entry.time, "sleep"):
        assert entry.refresh_entry_history(path) is False
    assert path.read_text() == "last good cache"


def test_adjusted_download_is_normalized_and_published(tmp_path):
    path = tmp_path / "daily.csv"
    frame = prices([100.] * 250)
    frame = frame.rename(columns={"NASDAQ": "^IXIC", "DOW": "^DJI"})
    raw = pd.concat({"Close": frame}, axis=1)
    with patch.object(entry.yf, "download", return_value=raw) as download, patch.object(entry.time, "sleep"):
        assert entry.refresh_entry_history(path)
    assert download.call_args.kwargs["auto_adjust"] is True
    assert download.call_args.kwargs["interval"] == "1d"
    assert len(entry.entry_history(path)) == 250
    assert download.call_args.kwargs["threads"] is False


def test_closed_market_cached_read_does_not_download(tmp_path):
    path = tmp_path / "daily.csv"
    frame = prices([100.] * 100)
    frame.index = pd.bdate_range(end="2026-07-24", periods=100)
    frame.to_csv(path)
    with patch.object(entry, "refresh_entry_history") as refresh:
        result = entry.get_entry_history(False, path, now=pd.Timestamp("2026-07-26 12:00Z"))
    refresh.assert_not_called()
    assert len(result) == 100
