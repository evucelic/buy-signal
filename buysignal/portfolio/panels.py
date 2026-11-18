"""The /portfolio deck: one caption and one chart per tab, from data read once per command."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from buysignal.analysis import portfolio_charts as charts
from buysignal.analysis.signal_charts import RANGES
from buysignal.portfolio import instruments, performance, plan, tax
from buysignal.telegram_bot.render import esc
from buysignal.telegram_bot.views import _lines, _pre, stamp

TABS = (("pf_value", "Value"), ("pf_returns", "Returns"), ("pf_mix", "Mix"),
        ("pf_risk", "Risk"), ("pf_trades", "Trades"), ("pf_cash", "Cash"),
        ("pf_tax", "Tax"), ("pf_valuation", "Valuation"))
FIRST = "pf_value"
RANGED = {"pf_value", "pf_returns"}
DEFAULT_RANGE = "1y"
PORTFOLIO_RANGES = ("3m", "1y", "max")
TARGET_MIX = "Target mix"
_SHORT = {"S&P 500": "S&P 500", TARGET_MIX: "Target mix", "All-World": "All-World"}
_RECENT = 6
_RECONCILE_TOLERANCE = 0.005  # holdings plus cash may differ from IBKR's total by rounding, not more


@dataclass
class Account:
    """Everything one /portfolio reply is built from."""
    positions: dict
    holdings: dict
    cash: dict
    nav: list
    deposits: list
    trades: list
    meta: dict
    targets: dict
    prices: dict
    quotes: list
    eurusd: float | None
    closes: dict = field(default_factory=dict)          # {symbol: daily closes in the listing currency}
    eurusd_history: pd.Series | None = None
    benchmarks: dict = field(default_factory=dict)      # {name: daily closes in euros}
    history_complete: bool = True
    cape: pd.DataFrame | None = None                    # Shiller's monthly CAPE history
    sleeves: list = field(default_factory=list)         # [(name, forward P/E)] from MSCI, the owner's ETFs first
    real_yield: tuple | None = None                     # (10-year TIPS yield, its date)
    sources: list = field(default_factory=list)         # as-of words for the Valuation tab
    today: date = field(default_factory=date.today)
    notes: list = field(default_factory=list)


def _euros(value: float) -> str:
    return f"{'−' if value < 0 else ''}€{abs(value):,.0f}"


def _signed(value: float) -> str:
    return f"{'+' if value >= 0 else '−'}€{abs(value):,.0f}"


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:+.{digits}%}".replace("-", "−")


def _day(value) -> str:
    """'9 Dec 26' with the day padded, so a column of dates lines up."""
    value = pd.Timestamp(value)
    return f"{value.day:>2} {value:%b %y}"


def _date(value) -> str:
    """'9 Dec 26', unpadded, for prose."""
    value = pd.Timestamp(value)
    return f"{value.day} {value:%b %y}"


def _fall(value: float | None) -> str:
    return "—" if value is None else f"{min(value, 0.):.1%}".replace("-", "−")


def _range_words(range_key: str, start) -> str:
    return RANGES[range_key]["words"] or f"since {start:%b %Y}"


def _head(title: str, *rest: str) -> str:
    return " · ".join([f"<b>{title}</b>", *(esc(part) for part in rest if part)])


def _base_cash(account: Account) -> float:
    return sum(plan.to_euros(float(amount), currency, account.eurusd) or 0 for currency, amount in account.cash.items())


def reconcile(account: Account) -> str | None:
    """Holdings plus cash against IBKR's total for the statement's last day; a message when they differ."""
    if not account.nav or not account.positions:
        return None
    day, _, total = account.nav[-1]
    if str(day) != account.meta.get("statement_to"):
        return None
    held = 0.
    for position in account.positions.values():
        rate = float(position["fx_rate"]) if position.get("fx_rate") else plan.to_euros(1., position["currency"], account.eurusd)
        if rate is None:
            return None
        held += float(position["value"]) * rate
    ours = held + _base_cash(account)
    if total and abs(ours / total - 1) > _RECONCILE_TOLERANCE:
        return f"holdings plus cash €{ours:,.0f} differ from IBKR's total €{total:,.0f}"
    return None


def _footer(account: Account) -> list[str]:
    """Data dates and the reconciliation result, then any warnings."""
    meta = account.meta
    parts = []
    if meta.get("statement_to"):
        parts.append(f"IBKR to {_date(meta['statement_to'])}")
        if meta.get("synced_at"):
            parts.append("read " + stamp(meta["synced_at"], short=True))
    else:
        parts.append("IBKR not read yet")
    priced = [q.get("observed_at") for q in account.quotes if q.get("observed_at")]
    if priced:
        parts.append("prices " + stamp(max(priced), short=True))
    mismatch = reconcile(account)
    if not mismatch and account.nav:
        parts.append("adds up ✓")
    lines = ["<i>" + esc(" · ".join(parts)) + "</i>"]
    notes = list(account.notes) + ([mismatch] if mismatch else [])
    return lines + ["⚠️ " + esc(note) for note in notes]


def _empty(title: str, text: str, account: Account) -> tuple[str, None]:
    return "\n".join([f"<b>{title}</b>", esc(text), *_footer(account)]), None


@dataclass
class History:
    value: pd.Series                # account value per day, base currency
    paid: pd.Series                 # deposits less withdrawals to date
    benchmarks: dict                # {name: closes in euros}, the target mix included as a value series


def _euro_closes(account: Account) -> dict:
    result = {}
    for symbol, series in account.closes.items():
        currency = account.holdings.get(symbol, {}).get("currency") or next((i.currency for i in instruments.all() if i.symbol == symbol), None)
        converted = performance.in_euros(series, currency, account.eurusd_history) if currency else None
        if converted is not None:
            result[symbol] = converted
    return result


def history(account: Account) -> History | None:
    if len(account.nav) < 2:
        return None
    value = pd.Series([total for _, _, total in account.nav], index=pd.to_datetime([day for day, _, _ in account.nav]), dtype=float)
    return History(value, performance.flows(account.deposits, value.index).cumsum(), dict(account.benchmarks))


def _comparisons(account: Account, past: History, value: pd.Series) -> dict[str, pd.Series]:
    """{benchmark: value had the range's starting value and every later deposit bought it that day}."""
    start = value.index[0]
    deposits = [(day, amount) for day, amount in account.deposits if pd.Timestamp(day) > start]
    opening = float(value.iloc[0])
    result = {}
    for name in ("S&P 500", "All-World"):
        prices = past.benchmarks.get(name)
        if prices is not None and len(prices):
            result[name] = performance.same_deposits_in(prices, deposits, value.index, start_value=opening).dropna()
    weights = {symbol: weight for symbol, weight in account.targets.items() if weight > 0}
    if weights:
        mixed = performance.same_deposits_in_mix(_euro_closes(account), weights, deposits, value.index, start_value=opening)
        if mixed is not None and len(mixed):
            result[TARGET_MIX] = mixed
    return {name: series for name, series in result.items() if len(series) > 1}


def _cut(series: pd.Series, range_key: str) -> pd.Series:
    return charts.window(series.dropna(), range_key)


def value_tab(account: Account, past: History | None, range_key: str) -> tuple[str, bytes | None]:
    if past is None:
        return _empty("Value", "IBKR has sent no daily account values yet.", account)
    value = _cut(past.value, range_key)
    start, end = value.index[0], value.index[-1]
    whole = range_key == "max"
    paid_in = float(past.paid[end] - past.paid[start])
    opening = float(value.iloc[0])
    if whole:  # the first day's own deposit is money in, not a starting balance
        first_flow = float(past.paid[start])
        paid_in, opening = paid_in + first_flow, opening - first_flow
    gain = float(value.iloc[-1]) - opening - paid_in
    flows = [(start, opening)] if opening > 0.5 else []
    # On All, a deposit dated before the first daily value is part of that first value.
    flows += [(max(pd.Timestamp(day), start), amount) for day, amount in account.deposits
              if pd.Timestamp(day) <= end and (whole or pd.Timestamp(day) > start)]
    mwr = performance.money_weighted(flows, float(value.iloc[-1]), end)
    rows = []
    if not whole or opening > 0.5:
        label = f"Start {_date(start)}" if not whole else f"Held {_date(start)}"
        rows.append((label, _euros(opening)))
    rows += [("Deposits", _signed(paid_in)), ("Market gain", _signed(gain)), ("Now", _euros(value.iloc[-1]))]
    compared = _comparisons(account, past, value)
    if compared:
        rows += [(), ("Same deposits in", "Value", "You")]
        for name, series in compared.items():
            rows.append((_SHORT[name], _euros(series.iloc[-1]), _signed(float(value.iloc[-1]) - float(series.iloc[-1]))))
    lines = [_head("Value", _range_words(range_key, start)),
             f"<b>{_euros(value.iloc[-1])}</b> · {_signed(gain)} market gain" + (f" · {_pct(mwr)} on money in" if mwr is not None else ""),
             _pre(_lines(rows))]
    if whole and opening > 0.5 and not account.history_complete:
        lines.append(f"<i>Held: money in before {_date(start)}, the oldest IBKR day read so far.</i>")
    lines += _footer(account)
    money_in = opening + past.paid.reindex(value.index) - (0. if whole else float(past.paid[start]))
    return "\n".join(lines), charts.value(value, money_in, compared, range_key)


def returns_tab(account: Account, past: History | None, range_key: str) -> tuple[str, bytes | None]:
    if past is None:
        return _empty("Returns", "IBKR has sent no daily account values yet.", account)
    value = _cut(past.value, range_key)
    deposits = [(day, amount) for day, amount in account.deposits if pd.Timestamp(day) > value.index[0]]
    cumulative = {"You": performance.rebase(_cut(performance.time_weighted(past.value, account.deposits), range_key))}
    for name, series in _comparisons(account, past, value).items():
        cumulative[name] = performance.time_weighted(series, deposits)
    days = (value.index[-1] - value.index[0]).days
    yearly = days > 400
    rows = [("", "Per yr" if yearly else "Return", "Worst", "Vol")]
    for name, series in cumulative.items():
        result = performance.annualised(series) if yearly else float(series.iloc[-1])
        vol = performance.volatility(series)
        rows.append((_SHORT.get(name, name), _pct(result), _fall(performance.max_drawdown(series)), "—" if vol is None else f"{vol:.0%}"))
    mine = cumulative["You"]
    lead = []
    if "S&P 500" in cumulative:
        gap = float(mine.iloc[-1] - cumulative["S&P 500"].iloc[-1])
        lead.append(f"{gap * 100:+.1f} pts vs S&P 500".replace("-", "−"))
    if TARGET_MIX in cumulative:
        gap = float(mine.iloc[-1] - cumulative[TARGET_MIX].iloc[-1])
        lead.append(f"{gap * 100:+.1f} vs target".replace("-", "−"))
    lines = [_head("Returns", _range_words(range_key, value.index[0]), "deposits removed"),
             "<b>" + esc(" · ".join(lead)) + "</b>" if lead else "", _pre(_lines(rows)),
             "<i>Worst: deepest fall from a high. Vol: yearly swing size.</i>", *_footer(account)]
    return "\n".join(line for line in lines if line), charts.returns(cumulative, range_key)


def mix_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    if mix is None:
        return _empty("Mix", "No holdings or prices to weigh.", account)
    targeted = bool(account.targets)
    rows = [("", "€", "Now", "Target", "Gap €") if targeted else ("", "€", "Now")]
    for row in mix["rows"]:
        name = "Cash" if row["symbol"] == instruments.CASH else row["symbol"]
        cells = (name, f"{row['value']:,.0f}", f"{row['weight']:.0%}")
        rows.append(cells + (f"{row['target']:.0%}", f"{row['gap']:+,.0f}".replace("-", "−")) if targeted else cells)
    head = _head("Mix", _euros(mix["total"]), f"next buy → {mix['destination']}" if mix.get("destination") else "")
    lines = [head, _pre(_lines(rows))]
    if not targeted:
        lines.append("Set a target: /target " + " ".join(f"{s} {100 // max(1, len(account.holdings))}" for s in account.holdings))
    lines += _footer(account)
    return "\n".join(lines), charts.mix(mix["rows"], mix["total"])


def risk_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    values = {row["symbol"]: row["value"] for row in (mix or {}).get("rows", []) if row["symbol"] != instruments.CASH}
    result = performance.risk(_euro_closes(account), values) if values else None
    if result is None:
        return _empty("Risk", "Not enough shared price history to measure risk.", account)
    symbols = result["symbols"]
    rows = [("", "Money", "Risk", "Vol/yr")]
    rows += [(s, f"{result['weights'][s]:.0%}", f"{result['risk'][s]:.0%}", f"{result['volatility'][s]:.0%}") for s in symbols]
    rows.append(("All", "", "", f"{result['portfolio_volatility']:.0%}"))
    corr = result["correlation"]
    table = []
    if len(symbols) > 1:
        table = [("",) + tuple(symbols)]
        table += [(a,) + tuple("1" if a == b else f"{corr.loc[a, b]:.2f}" for b in symbols) for a in symbols]
    bets = result["effective_bets"]
    lines = [_head("Risk", f"{bets:.1f} effective bets of {len(symbols)}" if bets else ""),
             _pre(_lines(rows) + (["", "Correlation", *_lines(table)] if table else [])),
             f"<i>Risk: share of the swings. Effective bets: equal, unrelated holdings with the same spread. "
             f"{result['sessions']} sessions.</i>", *_footer(account)]
    return "\n".join(lines), charts.risk(result)


def _fees(account: Account) -> tuple[float, float, int]:
    """(commissions in euros, euros bought, orders) over the stored purchases."""
    paid = bought = 0.
    count = 0
    for trade in account.trades:
        quantity = float(trade["quantity"])
        if quantity <= 0:
            continue
        rate = float(trade["fx_rate"]) if trade.get("fx_rate") else plan.to_euros(1., trade["currency"], account.eurusd)
        if rate is None:
            continue
        same = trade.get("commission_currency", trade["currency"]) == trade["currency"]
        paid += float(trade["commission"]) * (rate if same else 1.)
        bought += quantity * float(trade["price"]) * rate
        count += float(trade["commission"]) > 0
    return paid, bought, count


def trades_tab(account: Account) -> tuple[str, bytes | None]:
    card = plan.scorecard(account.trades, account.closes)
    if not card["rows"]:
        return _empty("Trades", "No purchases in the IBKR statements yet.", account)
    summary = card["summary"]
    rows = [("", "", "Paid", "vs month")]
    running = False
    for row in card["rows"][-_RECENT:]:
        mark = "" if row["complete"] or row["diff"] is None else "*"
        running = running or bool(mark)
        rows.append((_day(row["date"]), row["symbol"], f"{row['price']:,.2f}", "—" if row["diff"] is None else _pct(row["diff"]) + mark))
    paid, bought, orders = _fees(account)
    totals = []
    if summary:
        totals += [("Avg vs month", _pct(summary["mean"], 2)), ("Chance range", f"±{summary['chance']:.2%}"),
                   ("Below avg", f"{summary['below']} of {summary['count']}")]
    if bought:
        totals += [("Commissions", f"€{paid:,.2f}"), ("Of money bought", f"{paid / bought:.2%}"),
                   ("Per order", f"€{paid / max(orders, 1):,.2f}")]
    verdict = {"better than chance": "timing beat chance ✓", "worse than chance": "timing worse than chance",
               "within chance": "timing within chance"}[summary["verdict"]] if summary else "no finished month yet"
    lines = [_head("Trades", f"{card['count']} buys", verdict),
             _pre(_lines(rows) + (["* month still running"] if running else []) + [""] + _lines(totals)),
             "<i>vs month: your price against that month's average close.</i>", *_footer(account)]
    noises = [row["noise"] for row in card["rows"] if row.get("noise")]
    return "\n".join(lines), charts.timing(card["rows"], sum(noises) / len(noises) if noises else None)


def _days_to_invest(account: Account) -> float | None:
    """Median days from a deposit to the first purchase on or after it."""
    buys = sorted(date.fromisoformat(t["trade_date"]) for t in account.trades if float(t["quantity"]) > 0)
    waits = []
    for day, amount in account.deposits:
        later = next((buy for buy in buys if buy >= day), None)
        if amount > 0 and later is not None:
            waits.append((later - day).days)
    return float(pd.Series(waits).median()) if waits else None


def cash_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    total = _base_cash(account)
    deposits = pd.Series({pd.Timestamp(day): amount for day, amount in account.deposits}, dtype=float).sort_index()
    rows = []
    if len(deposits):
        year_ago = pd.Timestamp(account.today) - pd.DateOffset(years=1)
        rows += [("Deposited", _euros(deposits.sum())), ("Deposits", f"{(deposits > 0).sum()} · avg {_euros(deposits[deposits > 0].mean())}"),
                 ("Last 12 months", _euros(deposits[deposits.index > year_ago].sum())),
                 (f"In {account.today.year}", _euros(deposits[deposits.index.year == account.today.year].sum()))]
        wait = _days_to_invest(account)
        if wait is not None:
            rows.append(("Invested within", f"{wait:.0f} days (median)"))
        rows.append(())
    rows += [(f"Cash {currency}", f"{float(amount):,.2f}") for currency, amount in account.cash.items()]
    reserve = account.targets.get(instruments.CASH, 0.)
    if mix:
        rows.append(("To invest", _euros(max(0., total - reserve * mix["total"]))))
    if len(deposits):
        rows += [(), ("Recent",)] + [(_day(day), _signed(amount)) for day, amount in deposits.iloc[::-1].head(_RECENT).items()]
    since = f"since {_date(deposits.index[0])}" if len(deposits) else ""
    lines = [_head("Cash", f"{_euros(total)} at IBKR", f"{_euros(deposits.sum())} paid in {since}" if len(deposits) else ""),
             _pre(_lines(rows))]
    if not account.history_complete and len(deposits):
        lines.append(f"<i>Deposits before {_date(_history_from(account) or deposits.index[0])} are still being read from IBKR.</i>")
    lines += _footer(account)
    balance = None
    if len(account.nav) > 1:
        balance = pd.Series([cash for _, cash, _ in account.nav], index=pd.to_datetime([day for day, _, _ in account.nav]), dtype=float)
    return "\n".join(lines), charts.cash(deposits, balance)


def _history_from(account: Account) -> date | None:
    """Oldest day of IBKR history: the backfill's record, else the first daily value."""
    if account.meta.get("history_from"):
        return date.fromisoformat(account.meta["history_from"])
    return account.nav[0][0] if account.nav else None


def tax_tab(account: Account) -> tuple[str, bytes | None]:
    history_from = _history_from(account)
    lots, realised = tax.lots(account.trades, account.positions, account.prices, account.eurusd, account.eurusd_history, history_from)
    if not lots:
        return _empty("Tax", "No holdings to value.", account)
    result = tax.cash_out(lots, realised, account.today, history_from)
    cash = _base_cash(account)
    after = result["value"] + cash - result["tax"]
    rows = [("Holdings", _euros(result["value"])), ("Cost", _euros(result["cost"])), ("Gain", _signed(result["gain"])),
            (" taxed", _signed(result["taxable_gain"] - result["realised"])), (" tax-free", _signed(result["exempt_gain"]))]
    if result["realised"]:
        rows.append((f" sold in {account.today.year}", _signed(result["realised"])))
    rows += [(f"Tax {tax.RATE:.0%}", _signed(-result["tax"])), ("Cash", _euros(cash)), ("You keep", _euros(after))]
    upcoming = [(day, amount) for day, amount in result["schedule"][1:]]
    lines = [_head("Tax", "if you sold everything today"),
             f"<b>{_euros(result['tax'])} tax</b> · {_euros(after)} after tax", _pre(_lines(rows))]
    if upcoming:
        day, amount = upcoming[0]
        lines.append(f"Next lot turns 2 years {esc(_date(day))} → tax {esc(_euros(amount))}")
    if result["undated"] and not account.history_complete and history_from:
        held = ", ".join(f"{lot.quantity:,.0f} {lot.symbol}" for lot in result["undated"])
        lines.append(f"<i>{esc(held)} bought before {esc(_date(history_from))}: taxed here until IBKR history shows the date.</i>")
    lines.append("<i>12% on gains held ≤2 years, first in first out, losses offset. Sale fees left out.</i>")
    lines += _footer(account)
    return "\n".join(lines), charts.tax(result["schedule"], lots, account.today)


def valuation_tab(account: Account) -> tuple[str, bytes | None]:
    from buysignal.collectors import cape
    if account.cape is None or account.cape.empty:
        return _empty("Valuation", "Shiller's CAPE data is not available yet.", account)
    s = cape.summary(account.cape)
    rows = [("", "Now", "Median"), ("CAPE", f"{s['cape']:.1f}", f"{s['cape_median']:.1f}"),
            ("Earnings yield", f"{s['earnings_yield']:.1%}", ""), ("Real 10y bond", f"{s['real_bond']:.1%}", ""),
            ("Excess yield", f"{s['excess_yield']:.1%}", f"{s['excess_median']:.1%}")]
    lines = [_head("Valuation", "US stocks", f"CAPE {s['cape']:.1f}"),
             esc(f"CAPE higher than {s['cape_rank']:.0%} of months since {s['since']:%Y}."), _pre(_lines(rows))]
    outcome = s["outcome"]
    if outcome:
        low, high = s["excess_yield"] - s["band"], s["excess_yield"] + s["band"]
        lines.append(esc(f"Next 10 years after an excess yield of {low:.1%} to {high:.1%} "
                         f"({outcome['months']} months, {outcome['decades']} decades):"))
        lines.append(_pre(_lines([("Real, per yr", "Low", "Mid", "High"),
                                  ("Stocks", *(_pct(v) for v in outcome["stocks"])),
                                  ("Over bonds", *(_pct(v) for v in outcome["over_bonds"]))])))
    if account.sleeves:
        rate = account.real_yield[0] if account.real_yield else None
        sleeve = [("Forward", "P/E", "Earn", "Over")]
        for name, forward in account.sleeves:
            earned, over = plan.paid_to_hold(forward, rate)
            sleeve.append((name, f"{forward:.1f}" if forward else "—", "—" if earned is None else f"{earned:.1%}",
                           "—" if over is None else _pct(over)))
        lines.append(_pre(_lines(sleeve)))
    lines.append("<i>Excess yield: 1/CAPE minus the real 10y yield. Low/High: 10th/90th percentile. "
                 "Over: forward earnings yield minus 10y TIPS.</i>")
    lines.append("<i>" + esc(" · ".join([f"Shiller {s['date']:%b %Y}", *account.sources])) + "</i>")
    lines += ["⚠️ " + esc(note) for note in account.notes]
    return "\n".join(lines), charts.valuation(account.cape, s)


def view_name(base: str, range_key: str) -> str:
    return base if range_key == DEFAULT_RANGE else f"{base}@{range_key}"


def build(account: Account) -> dict:
    """{view: (caption, png or None)} for every tab; Value and Returns have one view per range."""
    base = account.meta.get("base_currency")
    if base and base != plan.REPORT_CURRENCY:
        account.notes.append(f"IBKR account values are in {base}, shown with a € sign")
    mix = plan.allocation(account.holdings, account.prices, account.eurusd, account.targets, account.cash)
    if mix is None and account.holdings:
        account.notes.append("a holding cannot be valued: a price or the EUR/USD rate is missing")
    past = history(account)
    views = {}
    for range_key in PORTFOLIO_RANGES:
        views[view_name("pf_value", range_key)] = value_tab(account, past, range_key)
        views[view_name("pf_returns", range_key)] = returns_tab(account, past, range_key)
    views.update({"pf_mix": mix_tab(account, mix), "pf_risk": risk_tab(account, mix), "pf_trades": trades_tab(account),
                  "pf_cash": cash_tab(account, mix), "pf_tax": tax_tab(account), "pf_valuation": valuation_tab(account)})
    return views


def keyboard(token: str, view: str) -> dict:
    def button(label, key, selected=False):
        return {"text": ("● " if selected else "") + label, "callback_data": f"r:{token}:{key}"}
    base, _, chosen = view.partition("@")
    rows = []
    if base in RANGED:
        rows.append([button(RANGES[key]["label"], view_name(base, key), (chosen or DEFAULT_RANGE) == key) for key in PORTFOLIO_RANGES])
    tabs = [button(label, view_name(name, chosen or DEFAULT_RANGE) if name in RANGED else name, name == base) for name, label in TABS]
    tabs.append(button("✕", "close"))
    rows += [tabs[i:i + 3] for i in range(0, len(tabs), 3)]
    return {"inline_keyboard": rows}
