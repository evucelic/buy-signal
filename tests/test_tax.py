"""Croatian tax on a full sale today: 12% of the net gain on lots held two years or less, first in first out."""
from datetime import date

import pytest

from buysignal.portfolio import tax

TODAY = date(2026, 11, 18)


def trade(day, quantity, price, symbol="VWCG", currency="EUR", commission="0", fx=None):
    row = {"trade_id": f"{symbol}{day}{quantity}", "symbol": symbol, "currency": currency, "trade_date": day,
           "quantity": str(quantity), "price": str(price), "commission": commission, "commission_currency": currency}
    return {**row, "fx_rate": fx} if fx else row


def position(quantity, cost, symbol="VWCG", currency="EUR", fx=None):
    return {symbol: {"quantity": str(quantity), "cost": str(cost), "currency": currency, "mark_price": "0",
                     **({"fx_rate": fx} if fx else {})}}


def test_only_lots_held_two_years_or_less_are_taxed():
    trades = [trade("2024-11-01", 10, 50), trade("2026-03-02", 10, 50)]

    lots, realised = tax.lots(trades, position(20, 1000), {"VWCG": 60.}, 1.1)
    result = tax.cash_out(lots, realised, TODAY, None)

    assert result["gain"] == pytest.approx(200) and result["exempt_gain"] == pytest.approx(100)
    assert result["tax"] == pytest.approx(.12 * 100)


def test_a_loss_on_a_young_lot_offsets_a_gain_on_another():
    trades = [trade("2026-01-05", 10, 50), trade("2026-02-02", 10, 70, symbol="AVWS")]
    positions = {**position(10, 500), **position(10, 700, symbol="AVWS")}

    lots, realised = tax.lots(trades, positions, {"VWCG": 60., "AVWS": 65.}, 1.1)

    assert tax.cash_out(lots, realised, TODAY, None)["tax"] == pytest.approx(.12 * (100 - 50))


def test_tax_falls_to_zero_the_day_after_each_lot_turns_two():
    lots, realised = tax.lots([trade("2025-06-10", 10, 50)], position(10, 500), {"VWCG": 60.}, 1.1)

    schedule = tax.cash_out(lots, realised, TODAY, None)["schedule"]

    assert schedule == [(TODAY, pytest.approx(12.)), (date(2027, 6, 11), 0.)]


def test_dollar_lots_are_costed_at_ibkrs_rate_on_the_trade_date_with_the_commission():
    trades = [trade("2026-03-17", 10, 100, symbol="VUAA", currency="USD", commission="2", fx="0.9")]

    lots, _ = tax.lots(trades, position(10, 1002, symbol="VUAA", currency="USD", fx="0.8"), {"VUAA": 125.}, 1.25)

    assert lots[0].cost == pytest.approx(1002 * .9) and lots[0].value == pytest.approx(1250 * .8)


def test_shares_bought_before_the_history_are_one_undated_lot_taxed_until_proven_old():
    lots, realised = tax.lots([trade("2026-03-02", 10, 50)], position(15, 750), {"VWCG": 60.}, 1.1)

    undated = next(lot for lot in lots if lot.bought is None)
    assert undated.quantity == 5 and undated.cost == pytest.approx(250)
    assert tax.cash_out(lots, realised, TODAY, date(2025, 11, 17))["tax"] == pytest.approx(.12 * 150)
    assert tax.cash_out(lots, realised, TODAY, date(2024, 11, 1))["tax"] == pytest.approx(.12 * 100)


def test_a_sale_this_year_counts_toward_the_years_net_gain():
    trades = [trade("2026-01-05", 10, 50), trade("2026-06-01", -5, 70)]

    lots, realised = tax.lots(trades, position(5, 250), {"VWCG": 50.}, 1.1)

    assert realised == [(date(2026, 6, 1), pytest.approx(100))]
    assert tax.cash_out(lots, realised, TODAY, None)["tax"] == pytest.approx(12.)
