"""The account against the owner's target mix, whole-share orders for its cash, and the entries made.

Arithmetic only; prices, cash and trades are handed in. Three questions. Where does the account
stand against the chosen mix, cash included. Which whole shares bring it closest to that mix with
the cash at the broker, after commissions and currency conversion. And did the days chosen to buy
help: each purchase price against the average close of that listing in the same calendar month,
which is what a buyer who did not look would have paid on average. One purchase lands about 2%
either side of that average by chance, so the summary carries the range chance alone would produce
for the number of purchases. None of it is a forecast.
"""
from __future__ import annotations

from decimal import Decimal
from itertools import combinations
import math

import pandas as pd

from buysignal.portfolio import instruments

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


def to_euros(amount: float, currency: str, eurusd: float | None) -> float | None:
    """An amount in euros; None for a currency the bot cannot convert (only EUR and USD are priced)."""
    if currency == REPORT_CURRENCY:
        return float(amount)
    if currency == "USD" and eurusd:
        return float(amount) / eurusd
    return None


def prices(quotes, positions: dict | None = None) -> dict:
    """{symbol: price in the listing currency}: the live quote, else IBKR's last mark price."""
    result = {symbol: float(row["mark_price"]) for symbol, row in (positions or {}).items() if row.get("mark_price")}
    result.update({q["symbol"]: float(q["price"]) for q in quotes if q.get("price") is not None})
    return result


def _currency(symbol: str) -> str | None:
    return next((i.currency for i in instruments.all() if i.symbol == symbol), None)


def allocation(holdings: dict, price: dict, eurusd: float | None, targets: dict | None = None,
               cash: dict | None = None) -> dict | None:
    """Value and weight of each listing and of cash in euros; with a target mix, each gap and where new cash goes.

    Returns None when nothing is held or a holding cannot be valued (no price, or a currency
    without an exchange rate): a partial total would misstate every weight.
    """
    targets = targets or {}
    rows = []
    for symbol in dict.fromkeys([*holdings, *(s for s in targets if s != instruments.CASH)]):
        value = 0.
        if symbol in holdings:
            currency = holdings[symbol].get("currency") or _currency(symbol)
            if price.get(symbol) is None:
                return None
            value = to_euros(float(holdings[symbol]["quantity"]) * price[symbol], currency, eurusd)
            if value is None:
                return None
        rows.append({"symbol": symbol, "value": value})
    cash_eur = 0.
    for currency, amount in (cash or {}).items():
        converted = to_euros(float(amount), currency, eurusd)
        if converted is None:
            return None
        cash_eur += converted
    if cash is not None:
        rows.append({"symbol": instruments.CASH, "value": cash_eur})
    total = sum(row["value"] for row in rows)
    if total <= 0:
        return None
    for row in rows:
        row["weight"] = row["value"] / total
        row["target"] = targets.get(row["symbol"], 0.) if targets else None
        row["gap"] = row["value"] - row["target"] * total if targets else None
    securities = [row for row in rows if row["symbol"] != instruments.CASH]
    destination = min(securities, key=lambda row: row["gap"])["symbol"] if targets and securities else None
    return {"rows": rows, "total": total, "invested": total - cash_eur, "cash": cash_eur,
            "destination": destination, "eurusd": eurusd}


def purchase_plan(cash, holdings: dict, price: dict, eurusd: float | None, targets: dict,
                  fees: dict | None = None, fx: tuple | None = None, max_fee_share: float = MAX_FEE_SHARE) -> dict | None:
    """Whole-share orders for the cash, with fees, for one, two, ... listings at a time.

    `cash` is {currency: amount} as held at the broker (a number means euros). A CASH target keeps
    that share of the whole account uninvested. For every group of listings in the mix, each gets
    one share and further shares go one at a time to whichever is furthest below its target, while
    the money lasts. Per number of orders the group ending closest to the mix is kept. The
    recommendation is the closest of those whose fees stay within `max_fee_share` of the money
    spent, else the cheapest: a fee is paid for certain, and a mix left slightly off is corrected by
    the next purchase. Cash already in a listing's currency is spent there without conversion.
    Returns None when a price or an exchange rate is missing.
    """
    fees, fx = fees or {}, fx or DEFAULT_FX
    cash = {REPORT_CURRENCY: float(cash)} if isinstance(cash, (int, float)) else {c: float(a) for c, a in cash.items()}
    securities = {s: w for s, w in targets.items() if s != instruments.CASH}
    symbols = list(dict.fromkeys([*securities, *holdings]))
    currency = {s: (holdings[s].get("currency") if s in holdings else None) or _currency(s) for s in symbols}
    if not securities or any(price.get(s) is None or to_euros(1, currency[s], eurusd) is None for s in symbols):
        return None
    euro_price = {s: to_euros(price[s], currency[s], eurusd) for s in symbols}
    held = {s: float(holdings[s]["quantity"]) * euro_price[s] if s in holdings else 0. for s in symbols}
    in_currency = {}
    for name, amount in cash.items():
        converted = to_euros(amount, name, eurusd)
        if converted is None:
            return None
        in_currency[name] = converted
    available = sum(in_currency.values())
    invested = sum(held.values())
    reserve = targets.get(instruments.CASH, 0.) * (invested + available)
    spendable = max(0., available - reserve)
    # The mix among the listings alone: a cash target is met by not spending, not by buying.
    share = sum(securities.values())
    mix = {s: securities.get(s, 0.) / share for s in symbols}

    def off_mix(values):
        total = sum(values.values())
        return max(abs(values[s] / total - mix[s]) for s in symbols) if total else None

    def commission(symbol, quantity):
        minimum, rate = fees.get(symbol) or DEFAULT_FEES.get(currency[symbol], DEFAULT_FEES[REPORT_CURRENCY])
        value = quantity * euro_price[symbol]
        return max(to_euros(minimum, currency[symbol], eurusd), rate * value)

    def costs(quantity):
        """Orders with their commissions, and the conversion fee for money not already in each currency."""
        orders, needed = [], {}
        for symbol, count in quantity.items():
            value, charged = count * euro_price[symbol], commission(symbol, count)
            orders.append({"symbol": symbol, "quantity": count, "price": price[symbol], "price_eur": euro_price[symbol],
                           "currency": currency[symbol], "value": value, "commission": charged})
            needed[currency[symbol]] = needed.get(currency[symbol], 0.) + value + charged
        short = {c: amount - in_currency.get(c, 0.) for c, amount in needed.items() if amount > in_currency.get(c, 0.) + 1e-9}
        conversion = {c: max(fx[0], fx[1] * amount) for c, amount in short.items()}
        for order in orders:
            # Each converted currency's fee is carried by its orders, in proportion to their size.
            part = order["value"] + order["commission"]
            order["conversion"] = conversion.get(order["currency"], 0.) * part / needed[order["currency"]]
            order["total"] = part + order["conversion"]
        return orders, sum(order["total"] for order in orders)

    options = {}
    goal = invested + spendable
    buyable = [s for s in symbols if mix[s] > 0]
    for size in range(1, len(buyable) + 1):
        for group in combinations(buyable, size):
            quantity = {s: 1 for s in group}
            orders, spent = costs(quantity)
            if spent > spendable:
                continue
            while True:
                # Furthest below its share of what the listings will be worth once the cash is in.
                wanted = sorted(group, key=lambda s: held[s] + quantity[s] * euro_price[s] - mix[s] * goal)
                step = next((s for s in wanted if costs({**quantity, s: quantity[s] + 1})[1] <= spendable), None)
                if step is None:
                    break
                quantity[step] += 1
            orders, spent = costs(quantity)
            charged = sum(order["commission"] + order["conversion"] for order in orders)
            option = {"orders": orders, "spent": spent, "fees": charged, "fee_share": charged / spent,
                      "left": available - spent,
                      "off_mix": off_mix({s: held[s] + quantity.get(s, 0) * euro_price[s] for s in symbols})}
            best = options.get(size)
            if best is None or (option["off_mix"], option["fees"]) < (best["off_mix"], best["fees"]):
                options[size] = option
    plan = {"cash": available, "spendable": spendable, "reserve": reserve, "off_mix_now": off_mix(held),
            "eurusd": eurusd, "max_fee_share": max_fee_share, "options": [], "chosen": None}
    if not options:
        return plan
    ranked = [options[size] for size in sorted(options)]
    within = [option for option in ranked if option["fee_share"] <= max_fee_share]
    chosen = min(within, key=lambda o: (o["off_mix"], o["fees"])) if within else min(ranked, key=lambda o: o["fee_share"])
    return {**plan, "options": ranked, "chosen": chosen}


def month_noise(closes: pd.Series) -> float | None:
    """Standard deviation of one close against its own month's average, over the listing's history."""
    if closes is None or closes.empty:
        return None
    groups = closes.groupby(closes.index.to_period("M"))
    relative = (closes / groups.transform("mean") - 1)[groups.transform("count") >= MIN_MONTH_CLOSES]
    if relative.index.to_period("M").nunique() < MIN_NOISE_MONTHS:
        return None
    return float(relative.std())


def scorecard(trades, closes: dict) -> dict:
    """Each purchase against its month's average close, and what chance alone would show.

    `trades` are the stored IBKR executions; sales are left out. A purchase in a month that has
    not finished is listed against the average so far and left out of the summary. `closes` maps
    symbol to a date-indexed series in the listing's trading currency.
    """
    rows = []
    paid = fees = Decimal(0)
    for item in sorted(trades, key=lambda t: (t["trade_date"], t["trade_id"])):
        quantity = Decimal(item["quantity"])
        if quantity <= 0:
            continue
        cost = quantity * Decimal(item["price"])
        same_currency = item.get("commission_currency", item["currency"]) == item["currency"]
        commission = Decimal(item["commission"]) if same_currency else None
        if commission is not None:
            paid, fees = paid + cost, fees + commission
        row = {"date": item["trade_date"], "symbol": item["symbol"], "quantity": float(quantity),
               "price": float(item["price"]), "currency": item["currency"], "average": None, "diff": None,
               "complete": False, "fee_share": float(commission / cost) if commission is not None and cost else None}
        series = closes.get(item["symbol"])
        if series is not None and not series.empty:
            month = pd.Timestamp(item["trade_date"]).to_period("M")
            within = series[series.index.to_period("M") == month]
            row["complete"] = bool(series.index[-1].to_period("M") > month) and len(within) >= MIN_MONTH_CLOSES
            if len(within):
                row["average"] = float(within.mean())
                row["diff"] = row["price"] / row["average"] - 1
            row["noise"] = month_noise(series)
        rows.append(row)
    judged = [row for row in rows if row["complete"] and row["diff"] is not None and row.get("noise")]
    summary = None
    if judged:
        count = len(judged)
        mean = sum(row["diff"] for row in judged) / count
        chance = 2 * math.sqrt(sum(row["noise"] ** 2 for row in judged)) / count
        summary = {"count": count, "mean": mean, "chance": chance,
                   "below": sum(row["diff"] < 0 for row in judged),
                   "verdict": "within chance" if abs(mean) <= chance else "better than chance" if mean < 0 else "worse than chance"}
    return {"rows": rows, "summary": summary, "fee_share": float(fees / paid) if paid else None,
            "fees": float(fees), "count": len(rows)}


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
