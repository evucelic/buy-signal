"""Tests for collectors/*.py: cache read/write/merge logic and freshness gating, fully offline.

Selenium/live-network scraping (collectors/fed_rate.py's browser-driving code) isn't
meaningfully unit-testable and is scoped out — only its pure-logic helpers are covered here.
"""

import os
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

import openpyxl
import pandas as pd
import pytest
import requests

from collectors import fed_rate, margin_debt, sectors, yield_curve
from collectors import market as market_mod
from collectors import vix as vix_mod
from collectors.freshness import last_modified, refreshed_today

# --- freshness ---------------------------------------------------------------


def test_refreshed_today_missing_file(tmp_path):
    assert refreshed_today(tmp_path / "nope.csv") is False


def test_refreshed_today_true(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("x")
    assert refreshed_today(f) is True


def test_refreshed_today_false_old_mtime(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("x")
    old = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(f, (old, old))
    assert refreshed_today(f) is False


def test_last_modified_missing_file(tmp_path):
    assert last_modified(tmp_path / "nope.csv") is None


def test_last_modified_returns_aware_utc_datetime(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("x")
    result = last_modified(f)
    assert result is not None
    assert result.tzinfo is not None
    assert abs((datetime.now(result.tzinfo) - result).total_seconds()) < 5


# --- collectors/vix.py ---------------------------------------------------------------


def test_refresh_vix_cache_first_run(tmp_path):
    path = tmp_path / "vix.csv"
    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    series = pd.Series([20.0, 21.0, 22.0], index=idx, name="Close")
    with patch("collectors.vix._download_close", return_value=series):
        assert vix_mod.refresh_vix_cache(path) is True
    df = pd.read_csv(path, index_col=0)
    assert len(df) == 3


def test_refresh_vix_cache_merge_dedup(tmp_path):
    path = tmp_path / "vix.csv"
    idx1 = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    pd.Series([20.0, 21.0], index=idx1, name="Close").to_csv(path)
    idx2 = pd.date_range("2026-01-01 01:00", periods=2, freq="h", tz="UTC")
    new_series = pd.Series([99.0, 23.0], index=idx2, name="Close")
    with patch("collectors.vix._download_close", return_value=new_series):
        vix_mod.refresh_vix_cache(path)
    df = pd.read_csv(path, index_col=0)
    assert len(df) == 3  # 01:00 overlapped, deduped
    assert df["Close"].iloc[1] == 99.0  # overlapping timestamp took the newer value


def test_refresh_vix_cache_no_data(tmp_path):
    path = tmp_path / "vix.csv"
    with patch("collectors.vix._download_close", return_value=None):
        assert vix_mod.refresh_vix_cache(path) is False
    assert not path.exists()


def test_load_latest_cached_vix_missing_file(tmp_path):
    assert vix_mod.load_latest_cached_vix(tmp_path / "nope.csv") is None


def test_load_latest_cached_vix_missing_close_column(tmp_path):
    path = tmp_path / "vix.csv"
    pd.DataFrame({"Other": [1, 2]}).to_csv(path)
    assert vix_mod.load_latest_cached_vix(path) is None


def test_load_latest_cached_vix_normal(tmp_path):
    path = tmp_path / "vix.csv"
    idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    pd.Series([20.0, 25.0], index=idx, name="Close").to_csv(path)
    assert vix_mod.load_latest_cached_vix(path) == 25.0


def test_load_latest_cached_vix_empty_file(tmp_path):
    path = tmp_path / "vix.csv"
    pd.DataFrame({"Close": []}).to_csv(path)
    assert vix_mod.load_latest_cached_vix(path) is None


def test_get_latest_vix_raises_when_no_cache_and_download_fails(tmp_path):
    path = tmp_path / "vix.csv"
    with patch("collectors.vix._download_close", return_value=None):
        with pytest.raises(RuntimeError):
            vix_mod.get_latest_vix(path)


def test_vix_change_pct_needs_two_distinct_days(tmp_path):
    path = tmp_path / "vix.csv"
    idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    pd.Series([20.0, 21.0], index=idx, name="Close").to_csv(path)
    assert vix_mod.vix_change_pct(path) is None


def test_vix_change_pct_two_days(tmp_path):
    path = tmp_path / "vix.csv"
    idx = list(pd.date_range("2026-01-01 14:00", periods=2, freq="h", tz="UTC")) + list(
        pd.date_range("2026-01-02 14:00", periods=2, freq="h", tz="UTC")
    )
    pd.Series([20.0, 22.0, 25.0, 27.5], index=pd.DatetimeIndex(idx), name="Close").to_csv(path)
    assert vix_mod.vix_change_pct(path) == pytest.approx(27.5 / 22.0 - 1)


def test_vix_change_pct_missing_file(tmp_path):
    assert vix_mod.vix_change_pct(tmp_path / "nope.csv") is None


# --- collectors/market.py ---------------------------------------------------------------


def test_market_refresh_cache_first_run(tmp_path):
    path = tmp_path / "market.csv"
    idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    df = pd.DataFrame({"SPY": [100, 101], "NASDAQ": [200, 201], "DOW": [300, 301]}, index=idx)
    with patch("collectors.market._download_closes", return_value=df):
        assert market_mod.refresh_market_cache(path) is True
    assert path.exists()


def test_market_latest_changes_math(tmp_path):
    path = tmp_path / "market.csv"
    dates = pd.date_range("2026-01-01 14:00", periods=25, freq="D", tz="UTC")
    df = pd.DataFrame(
        {"SPY": [100 + i for i in range(25)], "NASDAQ": [200] * 25, "DOW": [300] * 25}, index=dates
    )
    df.to_csv(path)
    changes = market_mod.latest_changes(path)
    spy = changes["SPY"]
    assert spy["daily"] == pytest.approx(124 / 123 - 1)
    assert spy["weekly"] == pytest.approx(124 / 119 - 1)
    assert spy["monthly"] == pytest.approx(124 / 103 - 1)


def test_market_latest_changes_intraday(tmp_path):
    path = tmp_path / "market.csv"
    base = pd.date_range("2026-01-01 14:00", periods=24, freq="D", tz="UTC")
    extra_bar = pd.to_datetime(["2026-01-24 15:00"], utc=True)  # 2nd bar on the last existing day
    idx = base.append(extra_bar)
    spy = [100 + i for i in range(24)] + [999]  # later same-day bar should win as "latest"
    df = pd.DataFrame({"SPY": spy, "NASDAQ": [200] * 25, "DOW": [300] * 25}, index=idx)
    df.to_csv(path)
    changes = market_mod.latest_changes(path)
    assert changes["SPY"]["daily"] == pytest.approx(999 / 122 - 1)


def test_market_latest_changes_missing_file(tmp_path):
    assert market_mod.latest_changes(tmp_path / "nope.csv") is None


def test_market_latest_changes_insufficient_history_raises(tmp_path):
    # Fewer distinct days than MARKET_MONTHLY_LOOKBACK_DAYS + 1 -- a real constraint of the
    # current design (relied on by the first-run full-history backfill, not a scenario that
    # should occur in practice, but this documents the actual behavior if it ever does).
    path = tmp_path / "market.csv"
    dates = pd.date_range("2026-01-01 14:00", periods=3, freq="D", tz="UTC")
    df = pd.DataFrame({"SPY": [100, 101, 102], "NASDAQ": [200, 201, 202], "DOW": [300, 301, 302]}, index=dates)
    df.to_csv(path)
    with pytest.raises(IndexError):
        market_mod.latest_changes(path)


def test_get_latest_market_changes_raises_when_no_cache_and_download_fails(tmp_path):
    path = tmp_path / "market.csv"
    with patch("collectors.market._download_closes", return_value=None):
        with pytest.raises(RuntimeError):
            market_mod.get_latest_market_changes(path)


# --- collectors/margin_debt.py ---------------------------------------------------------------


@pytest.mark.parametrize(
    "age_months,day,expected_tier",
    [
        (0, 5, "fresh"),
        (1, 5, "fresh"),
        (1, 25, "refresh_due_soon"),
        (1, 21, "refresh_due_soon"),  # exactly MARGIN_REFRESH_WINDOW_DAY, inclusive per `>=`
        (1, 20, "fresh"),  # one day before the checkpoint
        (2, 5, "fresh"),
        (2, 25, "stale"),
        (3, 5, "stale"),
    ],
)
def test_data_freshness_tiers(age_months, day, expected_tier):
    today = pd.Timestamp(year=2026, month=7, day=day)
    latest_month = today.replace(day=1) - pd.DateOffset(months=age_months)
    history = pd.DataFrame({"month": [latest_month]})
    tier, _ = margin_debt.data_freshness(history, today)
    assert tier == expected_tier


def test_margin_should_refresh_missing_file(tmp_path):
    assert margin_debt.should_refresh(tmp_path / "nope.csv") is True


def test_margin_should_refresh_already_done_today(tmp_path):
    path = tmp_path / "m.csv"
    pd.DataFrame({"month": [pd.Timestamp("2026-06-01")]}).to_csv(path, index=False)
    assert margin_debt.should_refresh(path, today=pd.Timestamp.now()) is False


def test_margin_should_refresh_false_when_still_fresh_but_not_checked_today(tmp_path):
    path = tmp_path / "m.csv"
    pd.DataFrame({"month": [pd.Timestamp("2026-06-01")]}).to_csv(path, index=False)
    old = (datetime.now() - timedelta(days=3)).timestamp()
    os.utime(path, (old, old))
    assert margin_debt.should_refresh(path, today=pd.Timestamp("2026-07-05")) is False


def test_margin_should_refresh_stale_triggers(tmp_path):
    path = tmp_path / "m.csv"
    pd.DataFrame({"month": [pd.Timestamp("2025-01-01")]}).to_csv(path, index=False)
    old = (datetime.now() - timedelta(days=5)).timestamp()
    os.utime(path, (old, old))
    assert margin_debt.should_refresh(path, today=pd.Timestamp("2026-07-26")) is True


def test_parse_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customer Margin Balances"
    ws.append(
        [
            "Year-Month",
            "Debit Balances in Customers' Securities Margin Accounts",
            "Free Credit Balances in Customers' Cash Accounts",
            "Free Credit Balances in Customers' Securities Margin Accounts",
        ]
    )
    ws.append(["2026-05", 1000, 500, 200])
    ws.append(["2026-06", 1100, 520, 210])
    buf = BytesIO()
    wb.save(buf)
    df = margin_debt._parse_xlsx(buf.getvalue())
    assert list(df.columns) == ["month", "debit_balances", "free_credit_cash", "free_credit_margin"]
    assert df["debit_balances"].tolist() == [1000, 1100]


def test_get_clearance_connection_error_hint():
    with patch("collectors.margin_debt.requests.get", side_effect=requests.exceptions.ConnectionError()):
        with pytest.raises(RuntimeError, match="docker compose up -d cfbypass"):
            margin_debt._get_clearance()


# --- collectors/sectors.py ---------------------------------------------------------------


def test_industry_market_caps_arithmetic(monkeypatch):
    class FakeSector:
        def __init__(self, key):
            self.overview = {"market_cap": 1000.0}
            self.industries = pd.DataFrame({"name": ["Ind A"], "market weight": [0.5]}, index=["ind-a"])

    monkeypatch.setattr(sectors.yf, "Sector", FakeSector)
    monkeypatch.setattr(sectors, "SECTOR_INDUSTY_MAPPING_LC", {"tech": "Technology"})
    df = sectors._industry_market_caps()
    assert df.iloc[0]["market_cap"] == 500.0


def test_industry_earnings_growth_weighted_average(monkeypatch):
    top_df = pd.DataFrame({"market weight": [0.6, 0.4]}, index=["AAA", "BBB"])

    class FakeIndustry:
        def __init__(self, key):
            self.top_companies = top_df

    class FakeTicker:
        def __init__(self, symbol):
            growth = 0.1 if symbol == "AAA" else 0.2
            self.earnings_estimate = pd.DataFrame({"growth": [growth]}, index=["+1y"])

    monkeypatch.setattr(sectors.yf, "Industry", FakeIndustry)
    monkeypatch.setattr(sectors.yf, "Ticker", FakeTicker)
    result = sectors._industry_earnings_growth("some-key")
    assert result == pytest.approx(0.6 * 0.1 + 0.4 * 0.2)


def test_industry_earnings_growth_skips_missing_growth_column(monkeypatch):
    top_df = pd.DataFrame({"market weight": [0.5, 0.5]}, index=["AAA", "BBB"])

    class FakeIndustry:
        def __init__(self, key):
            self.top_companies = top_df

    class FakeTicker:
        def __init__(self, symbol):
            if symbol == "AAA":
                self.earnings_estimate = pd.DataFrame({"other_col": [1]}, index=["+1y"])
            else:
                self.earnings_estimate = pd.DataFrame({"growth": [0.3]}, index=["+1y"])

    monkeypatch.setattr(sectors.yf, "Industry", FakeIndustry)
    monkeypatch.setattr(sectors.yf, "Ticker", FakeTicker)
    assert sectors._industry_earnings_growth("k") == 0.3


def test_industry_earnings_growth_no_top_companies(monkeypatch):
    class FakeIndustry:
        def __init__(self, key):
            self.top_companies = None

    monkeypatch.setattr(sectors.yf, "Industry", FakeIndustry)
    result = sectors._industry_earnings_growth("k")
    assert result != result  # NaN


def test_update_sector_data_fetch_exception_leaves_cache_untouched(tmp_path):
    path = tmp_path / "sectors.csv"
    with patch("collectors.sectors._industry_market_caps", side_effect=RuntimeError("boom")):
        error = sectors.update_sector_data(path)
    assert not path.exists()
    assert error is not None and "boom" in error


def test_update_sector_data_success_returns_none(tmp_path):
    path = tmp_path / "sectors.csv"
    with patch(
        "collectors.sectors._industry_market_caps",
        return_value=pd.DataFrame({"industry": ["X"], "key": ["x"], "market_cap": [1.0]}),
    ), patch("collectors.sectors._industry_earnings_growth", new=lambda key: 0.1):
        error = sectors.update_sector_data(path)
    assert error is None
    assert path.exists()


# --- collectors/fed_rate.py (non-Selenium logic only) -----------------------------------


def test_percentage_parsing():
    assert fed_rate._percentage("34.2%") == pytest.approx(0.342)


def test_fed_rate_should_refresh_missing_file(tmp_path):
    assert fed_rate.should_refresh(tmp_path / "nope.csv") is True


def test_update_fed_rate_data_failure_leaves_cache_untouched(tmp_path):
    path = tmp_path / "fedwatch.csv"
    with patch("collectors.fed_rate._fetch_with_retries", return_value=(None, "ConnectionError: boom")):
        error = fed_rate.update_fed_rate_data(path)
    assert not path.exists()
    assert error == "ConnectionError: boom"


# --- collectors/yield_curve.py ---------------------------------------------------------------

_FRED_CSV = (
    "observation_date,T10Y3M\n"
    "2026-08-18,0.85\n"
    "2026-08-19,\n"          # market holiday: blank observation
    "2026-08-20,0.82\n"
    "2026-08-21,0.86\n"
)


def _mock_fred_response():
    resp = MagicMock()
    resp.text = _FRED_CSV
    return resp


def test_fetch_history_parses_and_drops_holiday_blanks():
    with patch("collectors.yield_curve.requests.get", return_value=_mock_fred_response()):
        df = yield_curve._fetch_history()
    assert list(df.columns) == ["date", "spread"]
    assert len(df) == 3  # blank 08-19 row dropped
    assert df["spread"].iloc[-1] == pytest.approx(0.86)
    assert df["date"].is_monotonic_increasing


def test_update_yield_curve_data_writes_cache(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    with patch("collectors.yield_curve.requests.get", return_value=_mock_fred_response()), patch(
        "collectors.yield_curve.time.sleep"
    ):
        error = yield_curve.update_yield_curve_data(path)
    assert error is None
    history = yield_curve.yield_curve_history(path)
    assert len(history) == 3
    assert history["spread"].iloc[-1] == pytest.approx(0.86)


def test_update_yield_curve_data_failure_leaves_cache_untouched(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    with patch("collectors.yield_curve._fetch_with_retries", return_value=(None, "ConnectionError: boom")), patch(
        "collectors.yield_curve.time.sleep"
    ):
        error = yield_curve.update_yield_curve_data(path)
    assert not path.exists()
    assert error == "ConnectionError: boom"


def test_yield_curve_should_refresh_missing_file(tmp_path):
    assert yield_curve.should_refresh(tmp_path / "nope.csv") is True


def test_yield_curve_should_refresh_false_when_refreshed_today(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("date,spread\n2026-08-21,0.86\n")  # fresh mtime = now
    assert yield_curve.should_refresh(path) is False
