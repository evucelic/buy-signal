"""Tests for collectors/*.py: cache read/write/merge logic and freshness gating, fully offline.

Selenium/live-network scraping (collectors/fed_rate.py's browser-driving code) isn't
meaningfully unit-testable and is scoped out: only its pure-logic helpers are covered here.
"""

import os
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

import openpyxl
import pandas as pd
import pytest
import requests

from collectors import fed_rate, fred, margin_debt, sectors, valuations, yield_curve
from collectors import market as market_mod
from collectors import vix as vix_mod
from collectors.freshness import last_modified, refreshed_today
from config import MARKET_MONTHLY_LOOKBACK_DAYS, MARKET_WEEKLY_LOOKBACK_DAYS

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


def test_refresh_vix_cache_keeps_the_newer_value_for_an_overlapping_timestamp(tmp_path):
    path = tmp_path / "vix.csv"
    idx1 = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    pd.Series([20.0, 21.0], index=idx1, name="Close").to_csv(path)
    idx2 = pd.date_range("2026-01-01 01:00", periods=2, freq="h", tz="UTC")
    new_series = pd.Series([99.0, 23.0], index=idx2, name="Close")
    with patch("collectors.vix._download_close", return_value=new_series):
        vix_mod.refresh_vix_cache(path)
    df = pd.read_csv(path, index_col=0)
    assert len(df) == 3
    assert df["Close"].iloc[1] == 99.0


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


def test_vix_change_pct_compares_the_last_bar_of_each_day(tmp_path):
    path = tmp_path / "vix.csv"
    idx = list(pd.date_range("2026-01-01 14:00", periods=2, freq="h", tz="UTC")) + list(
        pd.date_range("2026-01-02 14:00", periods=2, freq="h", tz="UTC")
    )
    pd.Series([99.0, 20.0, 99.0, 25.0], index=pd.DatetimeIndex(idx), name="Close").to_csv(path)
    assert vix_mod.vix_change_pct(path) == pytest.approx(0.25)


def test_vix_change_pct_missing_file(tmp_path):
    assert vix_mod.vix_change_pct(tmp_path / "nope.csv") is None


# --- collectors/market.py ---------------------------------------------------------------


_IGNORED_CLOSE = 1.0


def _spy_closes(latest, yesterday, a_week_ago, a_month_ago):
    closes = [_IGNORED_CLOSE] * (MARKET_MONTHLY_LOOKBACK_DAYS + 1)
    closes[-1] = latest
    closes[-2] = yesterday
    closes[-1 - MARKET_WEEKLY_LOOKBACK_DAYS] = a_week_ago
    closes[-1 - MARKET_MONTHLY_LOOKBACK_DAYS] = a_month_ago
    return closes


def _market_cache(tmp_path, spy_closes, extra_same_day_close=None):
    path = tmp_path / "market.csv"
    index = pd.date_range("2026-01-01 14:00", periods=len(spy_closes), freq="D", tz="UTC")
    closes = list(spy_closes)
    if extra_same_day_close is not None:
        index = index.append(pd.DatetimeIndex([index[-1] + pd.Timedelta(hours=1)]))
        closes.append(extra_same_day_close)
    filler = [100.0] * len(closes)
    pd.DataFrame({"SPY": closes, "NASDAQ": filler, "DOW": filler}, index=index).to_csv(path)
    return path


def test_spy_closes_builder_places_each_lookback_at_its_own_offset():
    closes = _spy_closes(latest=1.0, yesterday=2.0, a_week_ago=3.0, a_month_ago=4.0)
    assert closes[-1] == 1.0
    assert closes[-2] == 2.0
    assert closes[-6] == 3.0
    assert closes[-22] == 4.0


def test_market_refresh_cache_first_run(tmp_path):
    path = tmp_path / "market.csv"
    idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    df = pd.DataFrame({"SPY": [100, 101], "NASDAQ": [200, 201], "DOW": [300, 301]}, index=idx)
    with patch("collectors.market._download_closes", return_value=df):
        assert market_mod.refresh_market_cache(path) is True
    assert path.exists()


def test_market_daily_change_compares_the_latest_close_to_the_previous_day(tmp_path):
    path = _market_cache(
        tmp_path, _spy_closes(latest=110.0, yesterday=100.0, a_week_ago=88.0, a_month_ago=50.0)
    )
    assert market_mod.latest_changes(path)["SPY"]["daily"] == pytest.approx(0.10)


def test_market_weekly_change_compares_the_latest_close_to_five_trading_days_back(tmp_path):
    path = _market_cache(
        tmp_path, _spy_closes(latest=110.0, yesterday=100.0, a_week_ago=88.0, a_month_ago=50.0)
    )
    assert market_mod.latest_changes(path)["SPY"]["weekly"] == pytest.approx(0.25)


def test_market_monthly_change_compares_the_latest_close_to_twenty_one_trading_days_back(tmp_path):
    path = _market_cache(
        tmp_path, _spy_closes(latest=110.0, yesterday=100.0, a_week_ago=88.0, a_month_ago=50.0)
    )
    assert market_mod.latest_changes(path)["SPY"]["monthly"] == pytest.approx(1.20)


def test_market_daily_change_uses_the_last_bar_of_the_day_not_the_first(tmp_path):
    path = _market_cache(
        tmp_path,
        _spy_closes(latest=100.0, yesterday=100.0, a_week_ago=100.0, a_month_ago=100.0),
        extra_same_day_close=150.0,
    )
    assert market_mod.latest_changes(path)["SPY"]["daily"] == pytest.approx(0.50)


def test_market_latest_changes_missing_file(tmp_path):
    assert market_mod.latest_changes(tmp_path / "nope.csv") is None


def test_market_latest_changes_raises_insufficient_history_below_the_monthly_lookback(tmp_path):
    path = _market_cache(tmp_path, [100.0] * MARKET_MONTHLY_LOOKBACK_DAYS)
    with pytest.raises(market_mod.InsufficientHistory):
        market_mod.latest_changes(path)


def test_market_latest_changes_succeeds_at_exactly_the_monthly_lookback(tmp_path):
    path = _market_cache(tmp_path, [100.0] * (MARKET_MONTHLY_LOOKBACK_DAYS + 1))
    assert market_mod.latest_changes(path)["SPY"]["monthly"] == pytest.approx(0.0)


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
        (1, 25, "fresh"),  # one-month lag is the normal caught-up state, checkpoint or not
        (1, 21, "fresh"),
        (1, 20, "fresh"),
        (2, 5, "fresh"),
        (2, 21, "refresh_due_soon"),  # exactly MARGIN_REFRESH_WINDOW_DAY, inclusive per `>=`
        (2, 25, "refresh_due_soon"),
        (2, 20, "fresh"),  # one day before the checkpoint
        (3, 5, "stale"),
        (3, 25, "stale"),
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
    assert error is not None
    assert "boom" in error


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


# --- collectors/fred.py + yield_curve.py ---------------------------------------------------------------

_FRED_CSV = (
    "observation_date,T10Y3M\n"
    "2026-08-18,0.85\n"
    "2026-08-19,\n"          # market holiday: blank observation
    "2026-08-20,0.82\n"
    "2026-08-21,0.86\n"
)


_FRED_SERIES_CSV = {
    "T10Y3M": _FRED_CSV,
    "DGS2": "observation_date,DGS2\n2026-08-18,3.90\n2026-08-20,3.88\n2026-08-21,3.92\n",
    # Fed funds publishes a business day behind, so its last observation is missing here.
    "DFF": "observation_date,DFF\n2026-08-18,4.33\n2026-08-20,4.33\n",
}


def _mock_fred_response():
    resp = MagicMock()
    resp.text = _FRED_CSV
    return resp


def _mock_fred_get(url, params=None, timeout=None):
    """requests.get stand-in serving each FRED series id its own CSV."""
    resp = MagicMock()
    resp.text = _FRED_SERIES_CSV[params["id"]]
    return resp


def test_fred_fetch_series_parses_and_drops_holiday_blanks():
    with patch("collectors.fred.requests.get", return_value=_mock_fred_response()):
        df = fred.fetch_series("T10Y3M")
    assert list(df.columns) == ["date", "value"]
    assert len(df) == 3
    assert df["value"].iloc[-1] == pytest.approx(0.86)
    assert df["date"].is_monotonic_increasing


def test_update_yield_curve_data_writes_cache(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    with patch("collectors.fred.requests.get", side_effect=_mock_fred_get), patch(
        "collectors.yield_curve.time.sleep"
    ):
        error = yield_curve.update_yield_curve_data(path)
    assert error is None
    history = yield_curve.yield_curve_history(path)
    assert len(history) == 3
    assert history["spread"].iloc[-1] == pytest.approx(0.86)
    assert pd.isna(history["policy_spread"].iloc[-1])
    assert history["policy_spread"].dropna().iloc[-1] == pytest.approx(3.88 - 4.33)


def _failing_policy_leg(series_id, label):
    """fetch_series_with_retries stand-in: the curve fetches fine, the 2y leg is down."""
    if series_id == "T10Y3M":
        return pd.DataFrame({"date": [pd.Timestamp("2026-08-21")], "value": [0.86]}), None
    return None, "HTTPError: 503"


def test_update_yield_curve_data_caches_curve_when_policy_legs_fail(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    with patch(
        "collectors.yield_curve.fetch_series_with_retries", side_effect=_failing_policy_leg
    ), patch("collectors.yield_curve.time.sleep"):
        error = yield_curve.update_yield_curve_data(path)

    assert error == "policy spread: HTTPError: 503"
    history = yield_curve.yield_curve_history(path)
    assert history["spread"].iloc[-1] == pytest.approx(0.86)
    assert "policy_spread" not in history.columns


def test_update_yield_curve_data_keeps_cached_policy_spread_when_legs_return_nothing(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("date,spread,policy_spread\n2026-08-21,0.80,-0.41\n")
    empty = pd.DataFrame({"date": pd.to_datetime([]), "value": []})

    def legs(series_id, label):
        if series_id == "T10Y3M":
            return pd.DataFrame({"date": [pd.Timestamp("2026-08-21")], "value": [0.86]}), None
        return empty, None

    with patch("collectors.yield_curve.fetch_series_with_retries", side_effect=legs), patch(
        "collectors.yield_curve.time.sleep"
    ):
        yield_curve.update_yield_curve_data(path)

    history = yield_curve.yield_curve_history(path)
    assert history["policy_spread"].iloc[-1] == pytest.approx(-0.41)


def test_update_yield_curve_data_survives_a_truncated_cache(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("")

    with patch(
        "collectors.yield_curve.fetch_series_with_retries", side_effect=_failing_policy_leg
    ), patch("collectors.yield_curve.time.sleep"):
        error = yield_curve.update_yield_curve_data(path)

    assert error == "policy spread: HTTPError: 503"
    assert yield_curve.yield_curve_history(path)["spread"].iloc[-1] == pytest.approx(0.86)


def test_update_yield_curve_data_keeps_cached_policy_spread_when_legs_fail(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("date,spread,policy_spread\n2026-08-21,0.80,-0.41\n")

    with patch(
        "collectors.yield_curve.fetch_series_with_retries", side_effect=_failing_policy_leg
    ), patch("collectors.yield_curve.time.sleep"):
        yield_curve.update_yield_curve_data(path)

    history = yield_curve.yield_curve_history(path)
    assert history["spread"].iloc[-1] == pytest.approx(0.86)
    assert history["policy_spread"].iloc[-1] == pytest.approx(-0.41)


def test_update_yield_curve_data_failure_leaves_cache_untouched(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    with patch(
        "collectors.yield_curve.fetch_series_with_retries", return_value=(None, "ConnectionError: boom")
    ), patch("collectors.yield_curve.time.sleep"):
        error = yield_curve.update_yield_curve_data(path)
    assert not path.exists()
    assert error == "ConnectionError: boom"


def test_yield_curve_should_refresh_missing_file(tmp_path):
    assert yield_curve.should_refresh(tmp_path / "nope.csv") is True


def test_yield_curve_should_refresh_false_when_todays_cache_has_every_column(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("date,spread,policy_spread\n2026-08-21,0.86,-0.41\n")
    assert yield_curve.should_refresh(path) is False


def test_yield_curve_should_refresh_when_todays_cache_predates_the_policy_column(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("date,spread\n2026-08-21,0.86\n")
    assert yield_curve.should_refresh(path) is True


def test_yield_curve_should_refresh_when_todays_cache_is_truncated(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    path.write_text("")
    assert yield_curve.should_refresh(path) is True


# --- collectors/valuations.py (non-Selenium logic only, like fed_rate) -----------------------

# Body text as Selenium reads it off a rendered MSCI index page (abridged).
_MSCI_BODY = (
    "MSCI World Small Cap Index\n"
    "Performance as of August 21, 2026\n"
    "Key facts\n"
    "Div Yld (%)\n1.94\n"
    "P/E\n26.68\n"
    "P/E Fwd\n16.29\n"
    "P/BV\n2.11\n"
    "Number of constituents\n3,877\n"
)


def test_parse_metrics_extracts_both_ratios_and_asof():
    trailing, fwd, asof = valuations._parse_metrics(_MSCI_BODY)
    assert trailing == pytest.approx(26.68)
    assert fwd == pytest.approx(16.29)
    assert asof == "August 21, 2026"


def test_parse_metrics_handles_thousands_separators():
    body = _MSCI_BODY.replace("26.68", "1,026.68")
    trailing, _, _ = valuations._parse_metrics(body)
    assert trailing == pytest.approx(1026.68)


def test_parse_metrics_raises_on_challenge_page():
    with pytest.raises(ValueError):
        valuations._parse_metrics("Challenge Validation\nPlease wait...")


def test_update_valuations_appends_one_row_per_segment_per_day(tmp_path):
    path = tmp_path / "valuations.csv"
    with patch("collectors.valuations._build_driver", return_value=MagicMock()), patch(
        "collectors.valuations._fetch_index_metrics", return_value=(26.68, 16.29, "July 31, 2026")
    ), patch("collectors.valuations.time.sleep"):
        assert valuations.update_valuations_data(path) is None
        assert valuations.update_valuations_data(path) is None
    history = valuations.valuations_history(path)
    assert len(history) == len(valuations.OPPORTUNITY_SEGMENTS)
    assert set(history["segment"]) == set(valuations.OPPORTUNITY_SEGMENTS)
    assert (history["asof"] == "July 31, 2026").all()


def test_update_valuations_partial_failure_writes_the_rest(tmp_path):
    path = tmp_path / "valuations.csv"
    small_code = valuations.OPPORTUNITY_SEGMENTS["world_small"]["msci_code"]

    def flaky_metrics(driver, msci_code):
        if msci_code == small_code:
            raise ValueError("P/E metrics not found on page")
        return 20.0, 15.0, "July 31, 2026"

    with patch("collectors.valuations._build_driver", return_value=MagicMock()), patch(
        "collectors.valuations._fetch_index_metrics", side_effect=flaky_metrics
    ), patch("collectors.valuations.time.sleep"):
        error = valuations.update_valuations_data(path)
    assert "world_small" in error
    assert "not found" in error
    history = valuations.valuations_history(path)
    assert len(history) == len(valuations.OPPORTUNITY_SEGMENTS) - 1
    assert "world_small" not in set(history["segment"])


def test_update_valuations_webdriver_failure_returns_error(tmp_path):
    path = tmp_path / "valuations.csv"
    with patch("collectors.valuations._build_driver", side_effect=RuntimeError("no chrome")), patch(
        "collectors.valuations.time.sleep"
    ):
        error = valuations.update_valuations_data(path)
    assert "webdriver" in error
    assert "no chrome" in error
    assert not path.exists()


def test_valuations_should_refresh_missing_file(tmp_path):
    assert valuations.should_refresh(tmp_path / "nope.csv") is True
