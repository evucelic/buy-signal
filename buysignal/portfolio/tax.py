"""Croatian tax on selling everything today.

Zakon o porezu na dohodak (NN 152/24): 12% on capital gains, fund units included (čl. 70 st. 4);
first in, first out (čl. 70 st. 5); nothing due on a lot sold more than two years after purchase
(čl. 67 st. 8); losses offset gains within the calendar year. Costs include the commission, in euros
at IBKR's rate on the trade date. Sale fees are left out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

RATE = 0.12
YEARS_FREE = 2


@dataclass
class Lot:
    symbol: str
    bought: date | None      # None: bought before the stored history began
    quantity: float
    cost: float              # euros, commission included
    local: float = 0.        # the same cost in the listing's currency, to split IBKR's cost basis
    value: float = 0.        # euros at today's price

    @property
    def gain(self) -> float:
        return self.value - self.cost

    def free_from(self, history_from: date | None) -> date | None:
        """First tax-free sale day; an undated lot counts from the start of the stored history."""
        bought = self.bought or history_from
        if bought is None:
            return None
        try:
            anniversary = bought.replace(year=bought.year + YEARS_FREE)
        except ValueError:  # 29 February
            anniversary = bought.replace(year=bought.year + YEARS_FREE, day=28)
        return anniversary + timedelta(days=1)


def _rate_to_eur(currency: str, day, stored: str | None, eurusd: pd.Series | None, fallback: float | None) -> float | None:
    """Euros per unit of `currency`: IBKR's own rate when stored, else the day's EUR/USD close."""
    if stored:
        return float(stored)
    if currency == "EUR":
        return 1.
    if currency != "USD":
        return None
    if eurusd is not None and len(eurusd):
        before = eurusd[eurusd.index <= pd.Timestamp(day)]
        if len(before):
            return 1 / float(before.iloc[-1])
    return 1 / fallback if fallback else None


def lots(trades: list[dict], positions: dict, prices: dict, eurusd_now: float | None,
         eurusd: pd.Series | None = None, history_from: date | None = None) -> tuple[list[Lot], list[tuple[date, float]]]:
    """(open lots valued today, [(sale day, taxable gain)]), first in, first out.

    Shares the stored trades do not explain become one undated lot, costed at IBKR's basis less the dated lots.
    """
    open_lots: dict[str, list[Lot]] = {}
    realised = []
    for trade in sorted(trades, key=lambda t: (t["trade_date"], t["trade_id"])):
        quantity, price = float(trade["quantity"]), float(trade["price"])
        day = date.fromisoformat(trade["trade_date"])
        fx = _rate_to_eur(trade["currency"], day, trade.get("fx_rate"), eurusd, eurusd_now)
        if fx is None:
            continue
        commission = float(trade["commission"])
        commission *= fx if trade.get("commission_currency", trade["currency"]) == trade["currency"] else 1.
        queue = open_lots.setdefault(trade["symbol"], [])
        if quantity > 0:
            local = quantity * price + commission / fx
            queue.append(Lot(trade["symbol"], day, quantity, local * fx, local))
            continue
        left, proceeds = -quantity, -quantity * price * fx - commission
        while left > 1e-9 and queue:
            lot = queue[0]
            used = min(left, lot.quantity)
            cost = lot.cost * used / lot.quantity
            if lot.free_from(None) is None or day < lot.free_from(None):
                realised.append((day, proceeds * used / -quantity - cost))
            lot.local -= lot.local * used / lot.quantity
            lot.quantity, lot.cost, left = lot.quantity - used, lot.cost - cost, left - used
            if lot.quantity <= 1e-9:
                queue.pop(0)
    result = []
    for symbol, position in positions.items():
        held = float(position["quantity"])
        queue = open_lots.get(symbol, [])
        known = sum(lot.quantity for lot in queue)
        fx_now = float(position["fx_rate"]) if position.get("fx_rate") else _rate_to_eur(position["currency"], date.today(), None, None, eurusd_now)
        if fx_now is None:
            continue
        if held - known > 1e-9:
            local = max(0., float(position["cost"]) - sum(lot.local for lot in queue))
            fx_then = _rate_to_eur(position["currency"], history_from or date.today(), None, eurusd, eurusd_now) or fx_now
            queue = [Lot(symbol, None, held - known, local * fx_then, local), *queue]
        price = prices.get(symbol, float(position.get("mark_price") or 0))
        for lot in queue:
            lot.value = lot.quantity * price * fx_now
            result.append(lot)
    return result, realised


def cash_out(open_lots: list[Lot], realised, today: date, history_from: date | None) -> dict:
    """Tax on selling everything today, and on each later day a lot turns two years old."""
    def taxable(lot, day):
        free = lot.free_from(history_from)
        return free is None or day < free

    this_year = sum(gain for day, gain in realised if day.year == today.year)

    def tax_on(day):
        net = this_year + sum(lot.gain for lot in open_lots if taxable(lot, day))
        return RATE * max(0., net), net

    tax, net = tax_on(today)
    upcoming = sorted({lot.free_from(history_from) for lot in open_lots if taxable(lot, today) and lot.free_from(history_from)})
    schedule = [(today, tax)] + [(day, tax_on(day)[0]) for day in upcoming]
    return {
        "value": sum(lot.value for lot in open_lots), "cost": sum(lot.cost for lot in open_lots),
        "gain": sum(lot.gain for lot in open_lots), "taxable_gain": net, "tax": tax,
        "exempt_gain": sum(lot.gain for lot in open_lots if not taxable(lot, today)),
        "realised": this_year, "schedule": schedule, "undated": [lot for lot in open_lots if lot.bought is None],
    }
