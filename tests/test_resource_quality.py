"""Data coverage, corporate-action reconciliation, cache budgets and direct source paths."""

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from buysignal.analysis import signal_charts
from buysignal.collectors import entry, margin_debt, sectors
from buysignal.resources import FORCE_REFRESH, force_refreshes
from buysignal.signals import sector_signal
from buysignal.signals.base import SubSignal


@pytest.fixture(autouse=True)
def source_clock(monkeypatch):
    monkeypatch.setattr(entry, '_utc_now', lambda: pd.Timestamp('2026-10-30 21:00Z'))


def frame(values):
    return pd.DataFrame({name: values for name in ("SPY", "NASDAQ", "DOW")}, index=pd.bdate_range(end="2026-10-30", periods=len(values)))


def test_incremental_prices_rebase_the_retained_prefix_after_a_dividend():
    old = frame(np.linspace(80, 100, 250))
    new = old.tail(5).copy() * .98
    # Today's price move is not an adjustment anchor.
    new.iloc[-1] = old.iloc[-1] * 1.05
    merged = entry._merge_series(old.SPY, new.SPY)
    assert len(merged) == 250
    assert merged.iloc[0] == pytest.approx(old.SPY.iloc[0] * .98)
    assert merged.iloc[-1] == pytest.approx(old.SPY.iloc[-1] * 1.05)


def test_nonuniform_price_revision_requires_full_download():
    old = frame(np.linspace(80, 100, 250))
    new = old.tail(5).copy()
    new.iloc[0] *= .98
    with pytest.raises(ValueError, match="Nonuniform"):
        entry._merge_series(old.SPY, new.SPY)


def test_recent_valid_daily_history_skips_a_repeat_network_request(tmp_path):
    path = tmp_path / "daily.csv"
    frame(np.linspace(80, 100, 250)).to_csv(path)
    with patch.object(entry, "refresh_entry_history") as refresh:
        entry.get_entry_history(True, path, now=pd.Timestamp("2026-11-01 12:00Z"))
    refresh.assert_not_called()


def test_force_refresh_overrides_the_live_cache_budget(tmp_path):
    path = tmp_path / "daily.csv"
    frame(np.linspace(80, 100, 250)).to_csv(path)
    with patch.object(entry, "refresh_entry_history", return_value=True) as refresh, force_refreshes():
        entry.get_entry_history(True, path, now=pd.Timestamp("2026-11-01 12:00Z"))
    refresh.assert_called_once_with(path)
    assert FORCE_REFRESH.get() is False


def test_daily_full_reconciliation_is_followed_by_a_small_request(tmp_path):
    path = tmp_path / "daily.csv"
    original = frame(np.linspace(80, 100, 250)).rename(columns={"NASDAQ": "^IXIC", "DOW": "^DJI"})
    raw = pd.concat({"Close": original}, axis=1)
    with patch.object(entry.yf, "download", side_effect=[raw, raw.tail(5)]) as download, patch.object(entry.time, "sleep"):
        assert entry.refresh_entry_history(path)
        assert entry.refresh_entry_history(path)
    assert [c.kwargs["period"] for c in download.call_args_list] == [entry.MARKET_BOOTSTRAP_PERIOD, "5d"]
    assert len(entry.entry_history(path)) == 250


def test_finra_margin_data_comes_from_its_official_workbook_and_nothing_else_is_accepted():
    workbook, page = MagicMock(content=b"PKworkbook"), MagicMock(content=b"<html>challenge</html>")
    with patch.object(margin_debt.requests, "get", return_value=workbook):
        assert margin_debt._fetch_xlsx_bytes() == b"PKworkbook"
    with patch.object(margin_debt.requests, "get", return_value=page), pytest.raises(ValueError, match="non-workbook"):
        margin_debt._fetch_xlsx_bytes()


def test_earnings_metadata_discloses_missing_heavy_constituent():
    industry = MagicMock()
    industry.top_companies = pd.DataFrame({"market weight": [.8, .2]}, index=["AAA", "BBB"])
    def ticker(symbol):
        result = MagicMock()
        result.earnings_estimate = pd.DataFrame() if symbol == "AAA" else pd.DataFrame({"growth": [.3], "numberOfAnalysts": [12]}, index=["+1y"])
        return result
    with patch.object(sectors.yf, "Industry", return_value=industry), patch.object(sectors.yf, "Ticker", side_effect=ticker):
        data = sectors._industry_earnings_data("industry")
    assert data["earnings_growth_estimate_1y"] == .3
    assert data["estimate_coverage"] == pytest.approx(.2)
    assert data["covered_companies"] == 1
    assert data["analysts_min"] == 12


def test_insufficient_earnings_coverage_cannot_produce_a_green_quorum():
    data = pd.DataFrame({"industry": list("ABCDE"), "market_cap": [1e12]*5,
                         "earnings_growth_estimate_1y": [.3]*5, "estimate_coverage": [.2]*5})
    with patch.object(sector_signal, "sector_performance", return_value=data):
        with pytest.raises(ValueError, match="coverage"):
            sector_signal.score()


def test_repeated_chart_uses_cached_png_with_a_bounded_budget(monkeypatch):
    with signal_charts._CACHE_LOCK:
        signal_charts._CACHE.clear()
        signal_charts._CACHE_BYTES = 0
    monkeypatch.setattr(signal_charts, "CHART_CACHE_MAX_BYTES", 12)
    monkeypatch.setattr(signal_charts, "_render_signal_chart", lambda s, r=None: b"12345678")
    first = SubSignal("vix", 20, "none", "", history=pd.DataFrame({"vix": [20.]}))
    second = SubSignal("vix", 21, "none", "", history=pd.DataFrame({"vix": [21.]}))
    assert signal_charts.render_signal_chart(first) == b"12345678"
    assert signal_charts.render_signal_chart(first) == b"12345678"
    assert len(signal_charts._CACHE) == 1
    signal_charts.render_signal_chart(second)
    assert len(signal_charts._CACHE) == 1
    assert signal_charts._CACHE_BYTES <= 12
    with signal_charts._CACHE_LOCK:
        signal_charts._CACHE.clear()
        signal_charts._CACHE_BYTES = 0


def test_vix_cache_age_cannot_hide_a_stale_source_session(tmp_path):
    from buysignal.collectors import vix
    path = tmp_path/'vix.csv'
    pd.Series([24.], index=pd.DatetimeIndex(['2026-10-30T20:00Z']), name='Close').to_csv(path)
    with patch.object(vix, 'refresh_vix_cache', return_value=False) as refresh:
        with pytest.raises(RuntimeError, match='expected session'):
            vix.get_latest_vix(path, now=pd.Timestamp('2026-11-02T15:00Z'))
    refresh.assert_called_once()


def test_vix_valid_weekend_cache_does_not_refetch(tmp_path):
    from buysignal.collectors import vix
    path = tmp_path/'vix.csv'
    pd.Series([24.], index=pd.DatetimeIndex(['2026-10-30T20:00Z']), name='Close').to_csv(path)
    with patch.object(vix, 'refresh_vix_cache') as refresh:
        assert vix.get_latest_vix(path, now=pd.Timestamp('2026-11-01T15:00Z')) == 24.
    refresh.assert_not_called()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 0., -1.])
def test_invalid_cached_vix_is_not_a_market_reading(tmp_path, value):
    from buysignal.collectors import vix
    path = tmp_path/'vix.csv'
    pd.DataFrame({'Close':[value]}).to_csv(path)
    assert vix.load_latest_cached_vix(path) is None


def test_closed_market_signal_rejects_stale_vix_without_refreshing():
    from buysignal.signals import vix_signal
    with patch.object(vix_signal, 'load_latest_cached_vix', return_value=35.), \
         patch.object(vix_signal, '_vix_session_is_current', return_value=False), \
         patch.object(vix_signal, 'get_latest_vix') as fetch:
        with pytest.raises(ValueError, match='expected session'):
            vix_signal.score(allow_refresh=False)
    fetch.assert_not_called()
