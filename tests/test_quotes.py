"""Prices for the held listings: cached, isolated per listing, and never silently converted."""
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from buysignal.portfolio import quotes
from buysignal.portfolio.instruments import resolve
from buysignal.portfolio.presentation import quote_block
from buysignal.portfolio.store import PortfolioStore

NOW = datetime(2026, 11, 5, 15, 30, tzinfo=timezone.utc)  # 16:30 Zagreb


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path / "portfolio.sqlite3")


def test_listing_currency_is_the_trading_currency_not_the_funds():
    assert (resolve("VUAA").yahoo, resolve("VUAA").currency) == ("VUAA.L", "USD")
    assert (resolve("VWCG").yahoo, resolve("VWCG").currency) == ("VWCG.DE", "EUR")


def test_a_failed_quote_keeps_the_last_price_and_its_original_time(store, monkeypatch):
    cached = {"symbol": "VUAA", "currency": "USD", "price": 150., "change": .01, "interval": "5m",
              "observed_at": "2026-11-05T15:00Z", "retrieved_at": "2026-11-05T15:01Z", "last_attempt": "2026-11-05T15:01Z"}
    store.save_quote("VUAA", cached)
    monkeypatch.setattr(quotes, "_fetch", Mock(side_effect=TimeoutError()))

    result = quotes.get(store, resolve("VUAA"), now=NOW)

    assert result["price"] == 150 and result["refresh_failed"]
    assert (result["observed_at"], result["retrieved_at"]) == (cached["observed_at"], cached["retrieved_at"])


def test_a_fresh_quote_is_served_from_the_cache(store, monkeypatch):
    fetch = Mock(side_effect=lambda item, now: {"symbol": item.symbol, "price": 150., "currency": item.currency,
                                                "retrieved_at": now.isoformat(), "last_attempt": now.isoformat()})
    monkeypatch.setattr(quotes, "_fetch", fetch)

    quotes.get(store, resolve("VUAA"), now=NOW)
    quotes.get(store, resolve("VUAA"), now=NOW)

    assert fetch.call_count == 1


def test_a_provider_currency_that_differs_from_the_listing_is_refused():
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame({"Close": [10., 11.]}, index=pd.date_range("2026-11-05T14:00Z", periods=2, freq="5min"))
    ticker.history_metadata = {"currency": "GBP"}

    with patch.object(quotes.yf, "Ticker", return_value=ticker), pytest.raises(ValueError, match="currency"):
        quotes._fetch(resolve("VUAA"), pd.Timestamp(NOW))


def test_one_broken_listing_does_not_block_the_others(store, monkeypatch):
    def get(store, item, **kwargs):
        if item.symbol == "VWCG":
            raise ValueError("damaged cache")
        return {"symbol": item.symbol, "currency": item.currency, "price": 100.}
    monkeypatch.setattr(quotes, "get", get)

    priced = [q["symbol"] for q in quotes.all_quotes(store, now=NOW) if q["price"] is not None]

    assert priced == ["VUAA", "AVWS"]


def test_the_pre_close_table_fits_a_phone_and_marks_cached_prices():
    rows = [{"symbol": "VUAA", "price": 151.25, "currency": "USD", "change": -.1, "observed_at": "2026-11-05T15:25Z", "interval": "5m"},
            {"symbol": "VWCG", "price": 61.05, "currency": "EUR", "change": .01, "observed_at": "2026-11-04T15:25Z", "interval": "5m",
             "refresh_failed": True}]

    text = quote_block(rows)

    table = text.split("<pre>")[1].split("</pre>")[0]
    assert max(map(len, table.splitlines())) <= 33
    assert "* Cached: VWCG" in text and "VWCG 2026-11-04" in text


def test_daily_history_survives_a_failed_refresh_marked_as_old(store):
    now = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)
    frame = pd.DataFrame({"Close": [1.09, 1.10]}, index=pd.to_datetime(["2026-11-12", "2026-11-13"]).tz_localize("UTC"))
    with patch.object(quotes.yf, "Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        assert quotes.eurusd(store, now=now) == (1.10, "2026-11-13", False)
        ticker.return_value.history.side_effect = TimeoutError("offline")

        assert quotes.eurusd(store, now=now + pd.Timedelta(hours=2)) == (1.10, "2026-11-13", True)


def test_the_checklist_log_takes_closes_only_from_a_download_made_now(store):
    now = datetime(2026, 11, 16, 21, tzinfo=timezone.utc)
    frame = pd.DataFrame({"Close": [60.1, 60.5]}, index=pd.to_datetime(["2026-11-13", "2026-11-16"]).tz_localize("UTC"))
    with patch.object(quotes.yf, "Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        assert quotes.session_closes(store, now=now)["VWCG"] == (60.5, "2026-11-16", "EUR")
        ticker.return_value.history.side_effect = TimeoutError("offline")

        assert quotes.session_closes(store, now=now) == {}  # a cached bar may have been taken while still trading


def test_the_real_yield_is_cached_and_a_failed_refresh_keeps_the_last_value(store):
    now = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-11-11", "2026-11-12"]), "value": [2.80, 2.86]})
    with patch("buysignal.collectors.fred.fetch_series", return_value=frame):
        assert quotes.real_yield(store, now=now) == (pytest.approx(.0286), "2026-11-12", False)
    with patch("buysignal.collectors.fred.fetch_series", side_effect=TimeoutError("offline")):
        assert quotes.real_yield(store, now=now + pd.Timedelta(days=1)) == (pytest.approx(.0286), "2026-11-12", True)


def test_the_overlap_with_new_york_respects_holidays_and_weekends():
    from buysignal.portfolio import sessions
    start, end = sessions.us_overlap(datetime(2026, 11, 12, 9, tzinfo=timezone.utc), "LSE")
    assert (start.hour, start.minute, end.hour, end.minute) == (14, 30, 16, 30)
    assert sessions.us_overlap(datetime(2026, 11, 26, 9, tzinfo=timezone.utc), "XETR") is None   # Thanksgiving
    assert sessions.us_overlap(datetime(2026, 11, 14, 9, tzinfo=timezone.utc), "XETR") is False  # a Saturday
