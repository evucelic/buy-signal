"""Where new cash goes under a chosen mix, and purchase prices against each month's average."""
from datetime import date, datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from buysignal.portfolio import commands, plan, presentation, quotes
from buysignal.portfolio.journal import PortfolioStore


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path / "portfolio.sqlite3")


def closes(start="2025-01-01", end="2026-11-13", level=100., swing=2.):
    """Business-day closes that alternate around a level, so every month averages the level."""
    index = pd.bdate_range(start, end)
    return pd.Series(level + swing * np.where(np.arange(len(index)) % 2, 1, -1), index=index)


def test_mix_is_validated_and_replaced_as_a_whole(store):
    assert store.targets() == {}
    assert "not set" in commands.handle("/target", store)
    assert commands.handle("/target VUAA 60 VWCG 20 AVWS 20", store) == "Mix set: VUAA 60% VWCG 20% AVWS 20%"
    assert "total 100" in commands.handle("/target VUAA 60 VWCG 20", store)
    assert "Name each ETF once" in commands.handle("/target VUAA 50 VUAA 50", store)
    assert "Choose VUAA" in commands.handle("/target SPY 100", store)
    assert "Use: /target" in commands.handle("/target VUAA sixty", store)
    assert store.targets() == {"VUAA": .6, "VWCG": .2, "AVWS": .2}  # rejected input changed nothing
    assert commands.handle("/target VUAA 70% VWCG 30%", store) == "Mix set: VUAA 70% VWCG 30%"
    assert store.targets() == {"VUAA": .7, "VWCG": .3}


def test_new_cash_goes_to_the_listing_furthest_below_its_share(store):
    store.record("VUAA", "10", "110", date(2026, 10, 5))
    store.record("VWCG", "10", "50", date(2026, 10, 5))
    prices = [{"symbol": "VUAA", "price": 110.}, {"symbol": "VWCG", "price": 50.}, {"symbol": "AVWS", "price": None}]
    bare = plan.allocation(store.holdings(), prices, 1.10)
    assert bare["destination"] is None and bare["total"] == pytest.approx(1500)
    assert [round(row["weight"], 3) for row in bare["rows"]] == [.667, .333]  # dollars converted to euros
    mixed = plan.allocation(store.holdings(), prices, 1.10, {"VUAA": .6, "VWCG": .4})
    assert mixed["destination"] == "VWCG" and [round(row["gap"]) for row in mixed["rows"]] == [100, -100]
    # An unheld listing in the mix needs no price and is the furthest behind.
    assert plan.allocation(store.holdings(), prices, 1.10, {"VUAA": .5, "VWCG": .3, "AVWS": .2})["destination"] == "AVWS"
    # No rate for a dollar listing, or no price for a held one: no weights at all.
    assert plan.allocation(store.holdings(), prices, None) is None
    assert plan.allocation(store.holdings(), [{"symbol": "VUAA", "price": 110.}], 1.10) is None
    assert plan.allocation({}, prices, 1.10) is None


def test_entries_are_judged_against_their_own_month_and_against_chance(store):
    history = {"VUAA": closes(), "VWCG": closes()}
    assert plan.month_noise(history["VUAA"]) == pytest.approx(.02, abs=.001)
    for day, price in ((date(2026, 8, 5), "99"), (date(2026, 9, 7), "100"), (date(2026, 10, 6), "98")):
        store.record("VUAA", "10", price, day, fees="2")
    store.record("VWCG", "10", "97", date(2026, 11, 4))      # its month has not finished
    voided = store.record("VUAA", "10", "150", date(2026, 10, 7))
    store.void(voided["id"])
    card = plan.scorecard(store.purchases(limit=1000), history)
    assert card["count"] == 4 and [row["complete"] for row in card["rows"]] == [True, True, True, False]
    summary = card["summary"]
    assert summary["count"] == 3 and summary["below"] == 2
    assert summary["mean"] == pytest.approx(-.01, abs=.002)
    assert summary["chance"] == pytest.approx(2 * .02 / 3 ** .5, abs=.002)  # about ±2.3% for three buys
    assert summary["verdict"] == "within chance"  # 1% cheaper on three buys is not evidence of skill
    assert card["fee_share"] == pytest.approx(6 / 3940)

    text = presentation.entries_message(card, stale=["VWCG"])
    assert text.startswith("<b>Your entries</b> · within chance\n<pre>")
    table = text.split("<pre>")[1].split("</pre>")[0].splitlines()
    assert table[0].split() == ["Month", "ETF", "Paid", "Mo", "avg", "Diff"]
    assert table[1].split()[:4] == ["Aug", "26", "VUAA", "99.00"] and table[1].split()[-1] in ("-1.0%", "-1.1%")
    assert table[4].endswith("*") and "* month still running" in table
    assert "Judged" in text and "Chance range" in text and "2 of 3" in text and "0.15%" in text
    assert max(map(len, table)) <= 32 and "older copy: VWCG" in text


def test_a_consistent_edge_over_many_buys_is_called_and_thin_history_is_not_judged(store):
    history = {"VUAA": closes()}
    for month in range(1, 13):
        store.record("VUAA", "1", "98", date(2025, month, 10))
    assert plan.scorecard(store.purchases(limit=1000), history)["summary"]["verdict"] == "better than chance"
    short = {"VUAA": closes(start="2025-09-01", end="2025-12-31")}
    card = plan.scorecard(store.purchases(limit=1000), short)
    assert card["summary"] is None  # four months cannot say how far a random day sits from the average
    assert "no finished month yet" in presentation.entries_message(card)
    assert plan.scorecard(store.purchases(limit=1000), {})["summary"] is None
    assert presentation.entries_message(plan.scorecard([], {})).startswith("No purchases recorded")


def test_daily_history_is_cached_and_survives_a_failed_refresh(store):
    now = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)
    frame = pd.DataFrame({"Close": [1.09, 1.10]}, index=pd.to_datetime(["2026-11-12", "2026-11-13"]).tz_localize("UTC"))
    with patch.object(quotes.yf, "Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        assert quotes.eurusd(store, now=now) == (1.10, "2026-11-13", False)
        assert quotes.eurusd(store, now=now) == (1.10, "2026-11-13", False)
        assert ticker.return_value.history.call_count == 1  # served from the cache within the hour
        ticker.return_value.history.side_effect = TimeoutError("offline")
        later = now + pd.Timedelta(hours=2)
        assert quotes.eurusd(store, now=later) == (1.10, "2026-11-13", True)  # the last rate, marked old
        series, stale = quotes.daily_closes(store, now=later)
        assert series == {} and stale == ["VUAA", "VWCG", "AVWS"]
    assert quotes.eurusd(PortfolioStore(store.path.with_name("other.sqlite3")), now=now, refresh=False) == (None, None, True)


def test_session_closes_come_only_from_a_download_made_now(store):
    now = datetime(2026, 11, 16, 21, tzinfo=timezone.utc)
    frame = pd.DataFrame({"Close": [60.1, 60.5]}, index=pd.to_datetime(["2026-11-13", "2026-11-16"]).tz_localize("UTC"))
    with patch.object(quotes.yf, "Ticker") as ticker:
        ticker.return_value.history.return_value = frame
        assert quotes.session_closes(store, now=now)["VWCG"] == (60.5, "2026-11-16", "EUR")
        assert quotes.session_closes(store, now=now)["VUAA"] == (60.5, "2026-11-16", "USD")
        assert ticker.return_value.history.call_count == 6  # a cached copy is never used: it may hold a bar still trading
        ticker.return_value.history.side_effect = TimeoutError("offline")
        assert quotes.session_closes(store, now=now) == {}  # a failed download logs nothing rather than the cached copy


QUOTES = [{"symbol": "VUAA", "price": 143.}, {"symbol": "VWCG", "price": 60.}, {"symbol": "AVWS", "price": 25.}]
MIX = {"VUAA": .6, "VWCG": .2, "AVWS": .2}


def test_an_order_costs_its_commission_and_the_conversion_of_dollars():
    from buysignal.portfolio.instruments import resolve
    euro = plan.order_cost(resolve("VWCG"), 5, 60., 1.10, {}, plan.DEFAULT_FX)
    assert euro == {"value": 300., "commission": 1.25, "conversion": 0., "total": 301.25}  # the minimum, no conversion
    large = plan.order_cost(resolve("VWCG"), 100, 60., 1.10, {}, plan.DEFAULT_FX)
    assert large["commission"] == pytest.approx(3.)  # 0.05% once it exceeds the minimum
    dollar = plan.order_cost(resolve("VUAA"), 2, 130., 1.10, {"VUAA": (2.20, .001)}, (.5, .0003))
    assert dollar["commission"] == pytest.approx(2.)  # the owner's own 2.20 USD minimum, in euros
    assert dollar["conversion"] == .5 and dollar["total"] == pytest.approx(262.5)  # the conversion minimum applies


def test_small_cash_buys_one_etf_and_large_cash_spreads_across_the_mix(store):
    store.record("VUAA", "10", "143", date(2026, 10, 5))
    small = plan.purchase_plan(500, store.holdings(), QUOTES, 1.10, MIX)
    assert [len(option["orders"]) for option in small["options"]] == [1, 2, 3]
    chosen = small["chosen"]
    assert len(chosen["orders"]) == 1 and chosen["orders"][0]["symbol"] in ("VWCG", "AVWS")  # never the one already overweight
    assert chosen["fee_share"] == pytest.approx(1.25 / chosen["spent"]) and chosen["fee_share"] <= plan.MAX_FEE_SHARE
    assert all(option["spent"] <= 500 and option["left"] >= 0 for option in small["options"])
    assert all(float(order["quantity"]).is_integer() and order["quantity"] >= 1 for option in small["options"] for order in option["orders"])
    assert small["options"][2]["fee_share"] > plan.MAX_FEE_SHARE  # three minimum fees on 500 are not worth it
    assert chosen["off_mix"] < small["off_mix_now"]

    large = plan.purchase_plan(20000, store.holdings(), QUOTES, 1.10, MIX)
    assert len(large["chosen"]["orders"]) == 3 and large["chosen"]["off_mix"] < .01
    assert large["chosen"]["fee_share"] < .002
    dollars = next(order for order in large["chosen"]["orders"] if order["symbol"] == "VUAA")
    assert dollars["conversion"] == pytest.approx(.0003 * (dollars["value"] + dollars["commission"]))

    # Dearer fees turn the same cash into fewer orders.
    costly = plan.purchase_plan(3000, {}, QUOTES, 1.10, MIX, {s: (10., .0005) for s in MIX})
    cheap = plan.purchase_plan(3000, {}, QUOTES, 1.10, MIX)
    assert len(costly["chosen"]["orders"]) < len(cheap["chosen"]["orders"]) == 3
    assert plan.purchase_plan(20, {}, QUOTES, 1.10, MIX)["chosen"] is None  # not one share after fees
    assert plan.purchase_plan(500, {}, QUOTES, None, MIX) is None  # no rate for the dollar listing
    assert plan.purchase_plan(500, {}, QUOTES, 1.10, {}) is None


def test_cash_message_lists_orders_fees_and_the_alternatives(store):
    store.record("VUAA", "10", "143", date(2026, 10, 5))
    result = plan.purchase_plan(3000, store.holdings(), QUOTES, 1.10, MIX)
    opens = datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc), datetime(2026, 11, 12, 16, 30, tzinfo=timezone.utc)
    text = presentation.cash_message(result, {"VUAA": opens, "AVWS": opens})
    assert text.startswith("<b>Buy with 3,000 EUR</b> · 3 orders\n<pre>")
    table = text.split("<pre>")[1].split("</pre>")[0].splitlines()
    assert table[0].split() == ["Qty", "Price", "EUR", "Fee"] and table[1].split()[2] == "143.00"  # the dollar price as quoted
    assert "of which FX" in text and "&gt;3" in text and "Off mix" in text
    assert max(map(len, table)) <= 33
    assert text.endswith("New York open 15:30–17:30 CET · VUAA, AVWS")
    assert "New York is closed today; wider quotes: VUAA" in presentation.cash_message(result, {"VUAA": None, "AVWS": False})
    assert "Not enough for one share" in presentation.cash_message(plan.purchase_plan(20, {}, QUOTES, 1.10, MIX))


def test_fees_start_from_the_brokers_published_rates(store):
    listing = commands.handle("/fees", store)
    assert listing.endswith("Interactive Brokers tiered, as published: VUAA, VWCG, AVWS, FX") and "1.70 USD" in listing
    fixed = commands.handle("/fees fixed", store)
    assert "VUAA   4.00 USD  0.050%" in fixed and "VWCG   3.00 EUR  0.050%" in fixed and fixed.endswith("published: FX")
    assert "AVWS   1.25 EUR  0.050%" in commands.handle("/fees tiered", store)
    updated = commands.handle("/fees VUAA 2.20 0.1", store)
    assert "VUAA   2.20 USD  0.100%\n" in updated
    store.record("VUAA", "2", "110", date(2026, 11, 2), fees="1.93")
    assert "VUAA   2.20 USD  0.100%  1.93\n" in commands.handle("/fees", store)
    assert "FX     2.00 EUR  0.002%" in commands.handle("/fees FX 2 0.002", store)
    assert store.fees() == {"VUAA": (2.2, .001), "VWCG": (1.25, .0005), "AVWS": (1.25, .0005), "FX": (2., .00002)}
    assert "Use: /fees" in commands.handle("/fees VUAA cheap", store)
    assert "Choose VUAA" in commands.handle("/fees SPY 1 0.05", store)


def test_the_overlap_with_new_york_respects_holidays_and_weekends():
    from buysignal.portfolio import sessions
    start, end = sessions.us_overlap(datetime(2026, 11, 12, 9, tzinfo=timezone.utc), "LSE")
    assert (start.hour, start.minute, end.hour, end.minute) == (14, 30, 16, 30)
    assert sessions.us_overlap(datetime(2026, 11, 26, 9, tzinfo=timezone.utc), "XETR") is None   # Thanksgiving
    assert sessions.us_overlap(datetime(2026, 11, 14, 9, tzinfo=timezone.utc), "XETR") is False  # a Saturday


def test_paid_to_hold_is_the_earnings_yield_over_the_real_bond_yield():
    assert plan.paid_to_hold(20., .02) == (pytest.approx(.05), pytest.approx(.03))
    assert plan.paid_to_hold(12.5, None) == (pytest.approx(.08), None)
    assert plan.paid_to_hold(None, .02) == (None, None)


def test_the_real_yield_is_cached_and_a_failed_refresh_keeps_the_last_value(store):
    now = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)
    frame = pd.DataFrame({"date": pd.to_datetime(["2026-11-11", "2026-11-12"]), "value": [2.80, 2.86]})
    with patch("buysignal.collectors.fred.fetch_series", return_value=frame) as fetch:
        assert quotes.real_yield(store, now=now) == (pytest.approx(.0286), "2026-11-12", False)
        quotes.real_yield(store, now=now)
        assert fetch.call_count == 1
    with patch("buysignal.collectors.fred.fetch_series", side_effect=TimeoutError("offline")):
        value, day, stale = quotes.real_yield(store, now=now + pd.Timedelta(days=1))
        assert (value, day, stale) == (pytest.approx(.0286), "2026-11-12", True)
