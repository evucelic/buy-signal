"""Where new cash goes and what the recorded entries cost: arithmetic on the journal, no IO.

Two questions the entry checklist cannot answer. Which ETF does this month's cash go to:
the one furthest below the owner's chosen mix, so the mix is held by buying alone. And did
choosing the day help: each purchase price against the average close of that listing in the same
calendar month, which is what a buyer who did not look would have paid on average. One purchase
lands about 2% either side of that average by chance, so the summary carries the range chance
alone would produce for the number of purchases recorded. Neither is a forecast.
"""
from __future__ import annotations

from decimal import Decimal
from itertools import combinations
import math

import pandas as pd

from buysignal.portfolio.instruments import INSTRUMENTS

REPORT_CURRENCY = "EUR"
MIN_MONTH_CLOSES = 10   # fewer sessions than this is not a month's average
MIN_NOISE_MONTHS = 6    # months of history needed to say how far a random day sits from the average
# Interactive Brokers' published commissions for European stocks and ETFs, read from
# interactivebrokers.ie on 2026-11-14: (minimum per order in the listing currency, share of the
# order value). Tiered orders also pay the exchange's and clearer's own fees, which are not here.
IBKR = {"tiered": {"EUR": (1.25, .0005), "USD": (1.70, .0005)},
        "fixed": {"EUR": (3.00, .0005), "USD": (4.00, .0005)}}
DEFAULT_FEES = IBKR["tiered"]
DEFAULT_FX = (0., .0003)   # its automatic conversion: 0.03% on the rate, no minimum (euros, share)
MAX_FEE_SHARE = .005    # an option costing more than this share of the money spent is not recommended


def allocation(holdings: dict, quotes, eurusd: float | None, targets: dict | None = None) -> dict | None:
    """Value and weight of each listing in euros; with a target mix, each gap and the cash destination.

    Returns None when nothing is held or a held listing cannot be valued (no price, or no exchange
    rate for a dollar listing): a partial total would misstate every weight.
    """
    targets = targets or {}
    prices = {q["symbol"]: q.get("price") for q in quotes}
    rows = []
    for instrument in INSTRUMENTS:
        held = holdings.get(instrument.symbol)
        if not held and instrument.symbol not in targets:
            continue
        value = 0.
        if held:
            price = prices.get(instrument.symbol)
            if price is None:
                return None
            value = float(held["quantity"]) * float(price)
            if instrument.currency == "USD":
                if not eurusd:
                    return None
                value /= eurusd
            elif instrument.currency != REPORT_CURRENCY:
                return None
        rows.append({"symbol": instrument.symbol, "value": value})
    total = sum(row["value"] for row in rows)
    if total <= 0:
        return None
    for row in rows:
        row["weight"] = row["value"] / total
        row["target"] = targets.get(row["symbol"], 0.) if targets else None
        row["gap"] = row["value"] - row["target"] * total if targets else None
    destination = min(rows, key=lambda row: row["gap"])["symbol"] if targets else None
    return {"rows": rows, "total": total, "destination": destination, "eurusd": eurusd}


def month_noise(closes: pd.Series) -> float | None:
    """Standard deviation of one close against its own month's average, over the listing's history."""
    if closes is None or closes.empty:
        return None
    groups = closes.groupby(closes.index.to_period("M"))
    relative = (closes / groups.transform("mean") - 1)[groups.transform("count") >= MIN_MONTH_CLOSES]
    if relative.index.to_period("M").nunique() < MIN_NOISE_MONTHS:
        return None
    return float(relative.std())


def scorecard(purchases, closes: dict) -> dict:
    """Each purchase against its month's average close, and what chance alone would show.

    A purchase in a month that has not finished is listed against the average so far and left out
    of the summary. `closes` maps symbol to a date-indexed series in the listing's trading currency.
    """
    rows, squares = [], 0.
    paid = fees = Decimal(0)
    for item in sorted(purchases, key=lambda p: (p["trade_date"], p["id"])):
        if item.get("voided_at"):
            continue
        cost = Decimal(item["quantity"]) * Decimal(item["price"])
        paid, fees = paid + cost, fees + Decimal(item["fees"])
        row = {"date": item["trade_date"], "symbol": item["symbol"], "price": float(item["price"]),
               "average": None, "diff": None, "complete": False,
               "fee_share": float(Decimal(item["fees"]) / cost) if cost else None}
        series = closes.get(item["symbol"])
        if series is not None and not series.empty:
            month = pd.Timestamp(item["trade_date"]).to_period("M")
            within = series[series.index.to_period("M") == month]
            row["complete"] = bool(series.index[-1].to_period("M") > month) and len(within) >= MIN_MONTH_CLOSES
            if len(within):
                row["average"] = float(within.mean())
                row["diff"] = row["price"] / row["average"] - 1
            noise = month_noise(series)
            row["noise"] = noise
        rows.append(row)
    judged = [row for row in rows if row["complete"] and row["diff"] is not None and row.get("noise")]
    summary = None
    if judged:
        count = len(judged)
        squares = sum(row["noise"] ** 2 for row in judged)
        mean = sum(row["diff"] for row in judged) / count
        chance = 2 * math.sqrt(squares) / count
        summary = {"count": count, "mean": mean, "chance": chance,
                   "below": sum(row["diff"] < 0 for row in judged),
                   "verdict": "within chance" if abs(mean) <= chance else "better than chance" if mean < 0 else "worse than chance"}
    return {"rows": rows, "summary": summary, "fee_share": float(fees / paid) if paid else None,
            "fees": float(fees), "count": len(rows)}


def _euro_prices(quotes, eurusd: float | None, symbols) -> dict | None:
    """Price of one share in euros for each symbol, or None if any cannot be priced."""
    prices = {q["symbol"]: q.get("price") for q in quotes}
    result = {}
    for instrument in INSTRUMENTS:
        if instrument.symbol not in symbols:
            continue
        price = prices.get(instrument.symbol)
        if price is None or (instrument.currency == "USD" and not eurusd) or instrument.currency not in ("USD", REPORT_CURRENCY):
            return None
        result[instrument.symbol] = float(price) / eurusd if instrument.currency == "USD" else float(price)
    return result


def order_cost(instrument, quantity: int, price_eur: float, eurusd: float | None, fees: dict, fx: tuple) -> dict:
    """What one order costs in euros: the shares, the broker's commission and the currency conversion."""
    minimum, rate = fees.get(instrument.symbol) or DEFAULT_FEES[instrument.currency]
    value = quantity * price_eur
    rate_to_eur = 1 / eurusd if instrument.currency == "USD" else 1.
    commission = max(minimum * rate_to_eur, rate * value)
    conversion = max(fx[0], fx[1] * (value + commission)) if instrument.currency != REPORT_CURRENCY else 0.
    return {"value": value, "commission": commission, "conversion": conversion, "total": value + commission + conversion}


def purchase_plan(cash: float, holdings: dict, quotes, eurusd: float | None, targets: dict,
                  fees: dict | None = None, fx: tuple | None = None, max_fee_share: float = MAX_FEE_SHARE) -> dict | None:
    """Whole-share orders for `cash` euros, with fees, for one, two, ... ETFs at a time.

    For every group of ETFs in the mix, each gets one share and further shares go one at a time to
    whichever is furthest below its target, while the money lasts. Per number of orders the group
    ending closest to the mix is kept. The recommendation is the closest of those whose fees stay
    within `max_fee_share` of the money spent, else the cheapest: a fee is paid for certain, and a
    mix left slightly off is corrected by the next purchase. Returns None when a price is missing.
    """
    fees, fx = fees or {}, fx or DEFAULT_FX
    symbols = set(targets) | set(holdings)
    prices = _euro_prices(quotes, eurusd, symbols)
    if prices is None or not targets:
        return None
    by_symbol = {i.symbol: i for i in INSTRUMENTS}
    held = {s: float(holdings[s]["quantity"]) * prices[s] if s in holdings else 0. for s in symbols}
    base = sum(held.values())

    def off_mix(values):
        total = sum(values.values())
        return max(abs(values[s] / total - targets.get(s, 0.)) for s in symbols) if total else None

    def cost(symbol, quantity):
        return order_cost(by_symbol[symbol], quantity, prices[symbol], eurusd, fees, fx)

    options = {}
    buyable = [s for s in (i.symbol for i in INSTRUMENTS) if targets.get(s, 0) > 0]
    for size in range(1, len(buyable) + 1):
        for group in combinations(buyable, size):
            quantity = {s: 1 for s in group}
            if sum(cost(s, 1)["total"] for s in group) > cash:
                continue
            while True:
                spent = sum(cost(s, q)["total"] for s, q in quantity.items())
                # Furthest below its share of what the portfolio will be worth once the cash is in.
                wanted = sorted(group, key=lambda s: held[s] + quantity[s] * prices[s] - targets[s] * (base + cash))
                step = next((s for s in wanted if spent - cost(s, quantity[s])["total"] + cost(s, quantity[s] + 1)["total"] <= cash), None)
                if step is None:
                    break
                quantity[step] += 1
            orders = [{"symbol": s, "quantity": q, "price_eur": prices[s], **cost(s, q)} for s, q in quantity.items()]
            spent = sum(order["total"] for order in orders)
            charged = sum(order["commission"] + order["conversion"] for order in orders)
            option = {"orders": orders, "spent": spent, "fees": charged, "fee_share": charged / spent, "left": cash - spent,
                      "off_mix": off_mix({s: held[s] + quantity.get(s, 0) * prices[s] for s in symbols})}
            best = options.get(size)
            if best is None or (option["off_mix"], option["fees"]) < (best["off_mix"], best["fees"]):
                options[size] = option
    if not options:
        return {"cash": cash, "options": [], "chosen": None, "off_mix_now": off_mix(held), "eurusd": eurusd}
    ranked = [options[size] for size in sorted(options)]
    within = [option for option in ranked if option["fee_share"] <= max_fee_share]
    chosen = min(within, key=lambda o: (o["off_mix"], o["fees"])) if within else min(ranked, key=lambda o: o["fee_share"])
    return {"cash": cash, "options": ranked, "chosen": chosen, "off_mix_now": off_mix(held), "eurusd": eurusd,
            "max_fee_share": max_fee_share}


def paid_to_hold(forward_pe: float | None, real_yield: float | None) -> tuple[float | None, float | None]:
    """(earnings yield, earnings yield minus the real bond yield), both as fractions.

    Next year's expected earnings over the price is what the companies are expected to earn on each
    euro paid; a government bond that keeps pace with inflation pays `real_yield` with no risk. The
    difference is what holding the shares pays extra. It ignores growth and is not a forecast.
    """
    if not forward_pe or forward_pe <= 0:
        return None, None
    earned = 1 / forward_pe
    return earned, None if real_yield is None else earned - real_yield
