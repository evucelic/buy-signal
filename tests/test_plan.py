"""The account against a target mix, whole-share orders for its cash, and entries against the month."""
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from buysignal.portfolio import commands, plan, presentation
from buysignal.portfolio.store import PortfolioStore

PRICES = {"VUAA": 143., "VWCG": 60., "AVWS": 25.}   # VUAA in dollars, the others in euros
MIX = {"VUAA": .6, "VWCG": .2, "AVWS": .2}
EURUSD = 1.10


def held(**quantities):
    currency = {"VUAA": "USD", "VWCG": "EUR", "AVWS": "EUR"}
    return {symbol: {"quantity": Decimal(q), "currency": currency[symbol], "cost": Decimal(q) * 50} for symbol, q in quantities.items()}


def closes(start="2025-01-01", end="2026-11-13", level=100., swing=2.):
    """Business-day closes alternating around a level, so every month averages the level."""
    index = pd.bdate_range(start, end)
    return pd.Series(level + swing * np.where(np.arange(len(index)) % 2, 1, -1), index=index)


def buy(day, symbol, quantity, price, commission="1.25", trade_id=None):
    return {"trade_id": trade_id or f"{day}{symbol}", "symbol": symbol, "currency": "EUR", "trade_date": day,
            "quantity": str(quantity), "price": str(price), "commission": commission, "commission_currency": "EUR"}


# --- allocation -----------------------------------------------------------------------------------


def test_weights_are_in_euros_and_include_cash():
    mix = plan.allocation(held(VUAA=10, VWCG=10), PRICES, EURUSD, cash={"EUR": Decimal(200)})

    assert mix["total"] == pytest.approx(1300 + 600 + 200)
    assert {row["symbol"]: round(row["weight"], 3) for row in mix["rows"]} == {"VUAA": .619, "VWCG": .286, "CASH": .095}


def test_new_cash_goes_to_the_listing_furthest_below_its_target():
    mix = plan.allocation(held(VUAA=10, VWCG=10), PRICES, EURUSD, MIX)  # 1,300 € + 600 €

    assert mix["destination"] == "AVWS"  # in the mix, not yet held
    assert {row["symbol"]: round(row["gap"]) for row in mix["rows"]} == {"VUAA": 160, "VWCG": 220, "AVWS": -380}


def test_no_weights_at_all_when_a_holding_cannot_be_valued():
    assert plan.allocation(held(VUAA=10), PRICES, None) is None            # no dollar rate
    assert plan.allocation(held(VUAA=10), {"VWCG": 60.}, EURUSD) is None   # no price
    assert plan.allocation({}, PRICES, EURUSD) is None


def test_a_live_quote_wins_over_ibkrs_last_mark_price():
    positions = {"VUAA": {"mark_price": "140"}, "VWCG": {"mark_price": "59"}}

    assert plan.prices([{"symbol": "VUAA", "price": 143.}, {"symbol": "VWCG", "price": None}], positions) == {"VUAA": 143., "VWCG": 59.}


# --- purchase plan ------------------------------------------------------------------------------


def test_orders_are_whole_shares_within_the_cash():
    result = plan.purchase_plan(500, held(VUAA=10), PRICES, EURUSD, MIX)

    orders = [order for option in result["options"] for order in option["orders"]]
    assert orders and all(isinstance(order["quantity"], int) and order["quantity"] >= 1 for order in orders)
    assert all(0 <= option["left"] and option["spent"] <= 500 for option in result["options"])


def test_small_cash_goes_to_one_underweight_listing_to_save_minimum_fees():
    chosen = plan.purchase_plan(500, held(VUAA=10), PRICES, EURUSD, MIX)["chosen"]

    assert len(chosen["orders"]) == 1 and chosen["orders"][0]["symbol"] in ("VWCG", "AVWS")
    assert chosen["fee_share"] <= plan.MAX_FEE_SHARE


def test_large_cash_spreads_across_the_mix():
    chosen = plan.purchase_plan(20000, held(VUAA=10), PRICES, EURUSD, MIX)["chosen"]

    assert len(chosen["orders"]) == 3 and chosen["off_mix"] < .01


def test_dearer_fees_mean_fewer_orders():
    cheap = plan.purchase_plan(3000, {}, PRICES, EURUSD, MIX)["chosen"]
    dear = plan.purchase_plan(3000, {}, PRICES, EURUSD, MIX, {s: (10., .0005) for s in MIX})["chosen"]

    assert len(dear["orders"]) < len(cheap["orders"])


def test_dollars_already_in_the_account_buy_the_dollar_listing_without_conversion():
    in_euros = plan.purchase_plan({"EUR": 2000}, {}, PRICES, EURUSD, {"VUAA": 1.})["chosen"]
    in_dollars = plan.purchase_plan({"USD": 2200}, {}, PRICES, EURUSD, {"VUAA": 1.})["chosen"]

    assert in_euros["orders"][0]["conversion"] > 0
    assert in_dollars["orders"][0]["conversion"] == 0


def test_a_cash_target_keeps_that_share_of_the_account_uninvested():
    result = plan.purchase_plan({"EUR": 1000}, held(VWCG=50), PRICES, EURUSD, {"VWCG": .5, "AVWS": .4, "CASH": .1})

    assert result["reserve"] == pytest.approx(.1 * (3000 + 1000))
    assert result["chosen"]["spent"] <= 600 and result["chosen"]["left"] >= 400


def test_no_plan_without_a_price_rate_or_mix():
    assert plan.purchase_plan(500, {}, PRICES, None, MIX) is None
    assert plan.purchase_plan(500, {}, {"VWCG": 60.}, EURUSD, MIX) is None
    assert plan.purchase_plan(500, {}, PRICES, EURUSD, {}) is None


def test_too_little_cash_for_one_share_after_fees_says_so():
    result = plan.purchase_plan(20, {}, PRICES, EURUSD, MIX)

    assert result["chosen"] is None
    assert "Not enough for one share" in presentation.orders_message(result, source="for")


def test_the_orders_message_fits_a_phone_and_names_new_yorks_hours():
    result = plan.purchase_plan(3000, held(VUAA=10), PRICES, EURUSD, MIX)
    opens = datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc), datetime(2026, 11, 12, 16, 30, tzinfo=timezone.utc)

    text = presentation.orders_message(result, source="IBKR cash", overlaps={"VUAA": opens})

    table = text.split("<pre>")[1].split("</pre>")[0].splitlines()
    assert max(map(len, table)) <= 33
    assert "New York open 15:30–17:30 CET · VUAA" in text


# --- entries ------------------------------------------------------------------------------------


def test_entries_are_judged_against_their_month_and_against_chance():
    trades = [buy("2026-08-05", "VWCG", 10, 99), buy("2026-09-07", "VWCG", 10, 100), buy("2026-10-06", "VWCG", 10, 98)]

    card = plan.scorecard(trades, {"VWCG": closes()})

    assert [row["diff"] for row in card["rows"]] == pytest.approx([-.01, 0, -.02], abs=.002)
    assert card["summary"]["count"] == 3 and card["summary"]["verdict"] == "within chance"


def test_a_purchase_in_a_running_month_is_listed_but_not_judged():
    card = plan.scorecard([buy("2026-11-04", "VWCG", 10, 97)], {"VWCG": closes()})

    assert card["rows"][0]["diff"] is not None and card["summary"] is None


def test_sales_are_not_entries():
    card = plan.scorecard([buy("2026-08-05", "VWCG", -10, 120)], {"VWCG": closes()})

    assert card["rows"] == []


def test_paid_to_hold_is_the_earnings_yield_over_the_real_bond_yield():
    assert plan.paid_to_hold(20., .02) == (pytest.approx(.05), pytest.approx(.03))
    assert plan.paid_to_hold(None, .02) == (None, None)


# --- settings by command --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path / "portfolio.sqlite3")


def test_target_command_sets_the_mix_and_rejected_input_changes_nothing(store):
    assert "Target mix set" in commands.handle("/target VUAA 55 VWCG 20 AVWS 20 CASH 5", store)

    assert "total 100" in commands.handle("/target VUAA 60 VWCG 20", store)
    assert "Use: /target" in commands.handle("/target VUAA sixty", store)
    assert store.targets() == {"VUAA": .55, "VWCG": .2, "AVWS": .2, "CASH": .05}


def test_fees_start_from_ibkrs_published_tiered_rates_and_can_be_overridden(store):
    assert "VUAA   1.70 USD" in commands.handle("/fees", store)
    assert "VUAA   4.00 USD" in commands.handle("/fees fixed", store)

    commands.handle("/fees VUAA 2.20 0.1", store)

    assert store.fees()["VUAA"] == (2.2, .001)
