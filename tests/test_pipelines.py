"""End-to-end tests across the collector -> signal -> rendered message seam, network faked."""

from unittest.mock import MagicMock, patch

import pandas as pd

import telegram_bot as tb
from collectors import market, sectors, vix, yield_curve
from signals import market_signal, sector_signal, vix_signal, yield_curve_signal

_TRADING_DAYS = 60
_CURVE = 0.85
_TWO_YEAR = 4.90
_FED_FUNDS = 4.33


def _observation_dates():
    return pd.bdate_range("2026-06-01", periods=_TRADING_DAYS)


def _fred_csv(series_id, value):
    rows = "".join(f"{day.date()},{value}\n" for day in _observation_dates())
    return f"observation_date,{series_id}\n{rows}"


def _fake_fred_get(url, params=None, timeout=None):
    served = {
        "T10Y3M": _fred_csv("T10Y3M", _CURVE),
        "DGS2": _fred_csv("DGS2", _TWO_YEAR),
        "DFF": _fred_csv("DFF", _FED_FUNDS),
    }
    response = MagicMock()
    response.text = served[params["id"]]
    return response


def _write_pre_schema_cache(path):
    rows = "".join(f"{day.date()},{_CURVE}\n" for day in _observation_dates())
    path.write_text(f"date,spread\n{rows}")


def _refresh(path):
    with patch("collectors.fred.requests.get", side_effect=_fake_fred_get), patch(
        "collectors.yield_curve.time.sleep"
    ):
        return yield_curve.update_yield_curve_data(path)


def _score_against(path):
    with patch(
        "signals.yield_curve_signal.yield_curve_history",
        side_effect=lambda: yield_curve.yield_curve_history(path),
    ):
        return yield_curve_signal.score()


def test_a_cache_predating_the_policy_column_renders_without_it(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    _write_pre_schema_cache(path)

    message = tb._format_subsignal(_score_against(path))

    assert "10y-3m" in message
    assert "2y-FFR" not in message


def test_a_cache_predating_the_policy_column_is_refreshed_on_the_same_day(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    _write_pre_schema_cache(path)

    assert yield_curve.should_refresh(path) is True


def test_refreshing_a_pre_schema_cache_puts_the_policy_column_in_the_report(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    _write_pre_schema_cache(path)

    assert _refresh(path) is None

    message = tb._format_subsignal(_score_against(path))

    assert "2y-FFR" in message
    assert f"{_TWO_YEAR - _FED_FUNDS:+.2f}pp" in message


def test_a_refreshed_cache_is_not_refreshed_again_the_same_day(tmp_path):
    path = tmp_path / "yieldcurve.csv"
    _write_pre_schema_cache(path)
    _refresh(path)

    assert yield_curve.should_refresh(path) is False


def _vix_bars(closes, start="2026-01-01 14:00"):
    index = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    return pd.Series(closes, index=index, name="Close")


def test_a_vix_download_reaches_the_signal_through_the_cache(tmp_path):
    path = tmp_path / "vix.csv"
    with patch("collectors.vix._download_close", return_value=_vix_bars([18.0, 33.0])):
        vix.refresh_vix_cache(path)
        with patch(
            "signals.vix_signal.get_latest_vix", new=lambda: vix.get_latest_vix(path)
        ), patch("signals.vix_signal.vix_change_pct", new=lambda: vix.vix_change_pct(path)):
            signal = vix_signal.score()

    assert signal.score == 33.0
    assert signal.state == "strong"
    assert "+83.3% vs prior close" in signal.detail


def test_a_market_download_reaches_the_signal_through_the_cache(tmp_path):
    path = tmp_path / "market.csv"
    index = pd.date_range("2026-01-01 14:00", periods=30, freq="D", tz="UTC")
    closes = [100.0] * 29 + [90.0]
    frame = pd.DataFrame({"SPY": closes, "NASDAQ": closes, "DOW": closes}, index=index)
    with patch("collectors.market._download_closes", return_value=frame):
        market.refresh_market_cache(path)

    with patch(
        "signals.market_signal.get_latest_market_changes", new=lambda: market.latest_changes(path)
    ):
        signal = market_signal.score()

    assert signal.state == "dip"
    assert signal.passes is True
    assert "SPY daily -10.0%" in signal.detail


def test_a_sector_fetch_reaches_the_signal_through_the_cache(tmp_path):
    path = tmp_path / "sectors.csv"
    ranked = pd.DataFrame(
        {
            "key": ["a", "b", "c", "d", "e"],
            "industry": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"],
            "market_cap": [5e12, 4e12, 3e12, 2e12, 1e12],
        }
    )
    growth = {"a": 0.2, "b": 0.1, "c": 0.05, "d": -0.1, "e": -0.2}
    with patch("collectors.sectors._industry_market_caps", return_value=ranked), patch(
        "collectors.sectors._industry_earnings_growth", new=growth.get
    ):
        assert sectors.update_sector_data(path) is None

    with patch(
        "signals.sector_signal.sector_performance", new=lambda: sectors.sector_performance(path)
    ):
        signal = sector_signal.score()

    assert signal.state == "growing"
    assert signal.passes is True
    assert "3/5 top industries with rising EPS estimates" in signal.detail
    assert "Alpha: cap 5.00T" in signal.detail
