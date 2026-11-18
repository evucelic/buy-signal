"""The /portfolio deck: how the account is doing, each tab a caption and a chart.

Overview (money in, value out, the same deposits in a benchmark), Returns (deposits removed),
Mix (against the target), Risk (where the swings come from), Entries (was the timing good),
Fees, Cash, and P/E. Everything is computed from an `Account` read once when the command runs;
buttons only switch between what was prepared then, so a tap never waits on IBKR or Yahoo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from buysignal.analysis import portfolio_charts as charts
from buysignal.analysis.signal_charts import RANGES
from buysignal.portfolio import instruments, performance, plan
from buysignal.telegram_bot.render import esc
from buysignal.telegram_bot.views import _lines, _pre, stamp

TABS = (("pf_overview", "Overview"), ("pf_returns", "Returns"), ("pf_mix", "Mix"), ("pf_risk", "Risk"),
        ("pf_entries", "Entries"), ("pf_fees", "Fees"), ("pf_cash", "Cash"), ("pf_pe", "P/E"))
RANGED = {"pf_overview", "pf_returns"}
DEFAULT_RANGE = "1y"
PORTFOLIO_RANGES = ("3m", "1y", "max")
_ENTRY_ROWS = 6


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
    benchmark: pd.Series | None = None                 # daily closes in euros
    benchmark_name: str = "VWCE"
    valuations: str | None = None                      # the P/E tab's caption, rendered by the caller
    valuation_png: bytes | None = None
    notes: list = field(default_factory=list)


def _euros(value: float) -> str:
    return f"€{value:,.0f}"


def _signed(value: float) -> str:
    return f"{'+' if value >= 0 else '−'}€{abs(value):,.0f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}"


def _footer(account: Account) -> list[str]:
    when = account.meta.get("statement_to")
    priced = [q.get("observed_at") for q in account.quotes if q.get("observed_at")]
    parts = [f"IBKR {when}" if when else "IBKR not read yet"]
    if priced:
        parts.append("prices " + stamp(max(priced), short=True))
    if account.eurusd:
        parts.append(f"EUR/USD {account.eurusd:.4f}")
    return ["🕒 " + esc(" · ".join(parts)), *("⚠️ " + esc(note) for note in account.notes)]


def _range_words(range_key: str, start) -> str:
    return RANGES[range_key]["words"] or f"since {start:%b %Y}"


# --- the series the money and return tabs share --------------------------------------------------


@dataclass
class History:
    value: pd.Series                # account value per day, base currency
    paid: pd.Series                 # deposits less withdrawals to date
    twr: pd.Series                  # cumulative time-weighted return
    bench_twr: pd.Series | None
    bench_value: pd.Series | None   # the same deposits in the benchmark


def history(account: Account) -> History | None:
    if len(account.nav) < 2:
        return None
    value = pd.Series([total for _, _, total in account.nav], index=pd.to_datetime([day for day, _, _ in account.nav]), dtype=float)
    flow = performance.flows(account.deposits, value.index)
    twr = performance.time_weighted(value, account.deposits)
    bench_twr = bench_value = None
    if account.benchmark is not None and len(account.benchmark):
        bench_twr = performance.benchmark_return(account.benchmark, value.index)
        start = float(value.iloc[0]) - float(flow.iloc[0])
        bench_value = performance.same_deposits_in(account.benchmark, account.deposits, value.index, start_value=start)
    return History(value, flow.cumsum(), twr, bench_twr, bench_value)


def _cut(series: pd.Series | None, range_key: str) -> pd.Series | None:
    return None if series is None else charts.window(series.dropna(), range_key)


def overview(account: Account, past: History | None, range_key: str, mix: dict | None) -> tuple[str, bytes | None]:
    lines = ["💼 <b>Portfolio</b>" + (f" · {_euros(mix['total'])}" if mix else "")]
    if past is None:
        return "\n".join(lines + ["IBKR has sent no daily account values yet.", *_footer(account)]), None
    value = _cut(past.value, range_key)
    start, end = value.index[0], value.index[-1]
    whole = range_key == "max"
    opening = 0. if whole else float(value.iloc[0])
    paid_in = float(past.paid[end]) if whole else float(past.paid[end] - past.paid[start])
    if whole:
        opening = float(value.iloc[0]) - float(past.paid[start])  # whatever was there before the first deposit seen
    gain = float(value.iloc[-1]) - opening - paid_in
    money = opening + paid_in
    lines[0] += f" · {esc(_range_words(range_key, start))}"
    lines.append(f"<b>{_signed(gain)}</b> from the market" + (f" · {gain / money:+.1%} of the money in" if money > 0 else ""))
    rows = [("Value now", _euros(value.iloc[-1]))]
    if not whole:
        rows.append((f"Value {start:%-d %b %y}", _euros(opening)))
    rows += [("Paid in", _signed(paid_in)), ("Market gain", _signed(gain))]
    bench = _cut(past.bench_value, range_key) if whole and past.bench_value is not None else None
    if bench is not None and len(bench.dropna()):
        name = account.benchmark_name
        rows += [(), (f"Same in {name}", _euros(bench.dropna().iloc[-1])),
                 (f"You vs {name}", _signed(float(value.iloc[-1]) - float(bench.dropna().iloc[-1])))]
    lines.append(_pre(_lines(rows)))
    if bench is not None:
        lines.append(f"<i>Orange: every deposit put into {esc(account.benchmark_name)} on the day it arrived.</i>")
    elif past.bench_value is not None:
        lines.append("<i>Green: value above the money paid in; red: below. All shows the benchmark.</i>")
    lines += _footer(account)
    return "\n".join(lines), charts.overview(past.value, past.paid, bench, range_key, account.benchmark_name)


def returns(account: Account, past: History | None, range_key: str) -> tuple[str, bytes | None]:
    title = "📈 <b>Returns</b> · deposits removed"
    if past is None:
        return title + "\nIBKR has sent no daily account values yet.\n" + "\n".join(_footer(account)), None
    mine = performance.rebase(_cut(past.twr, range_key))
    theirs = performance.rebase(_cut(past.bench_twr, range_key)) if past.bench_twr is not None else None
    if theirs is not None and theirs.empty:
        theirs = None
    name = account.benchmark_name
    rows = [("", "You", name) if theirs is not None else ("", "You")]

    def row(label, measure):
        cells = (label, _pct(measure(mine)))
        rows.append(cells + (_pct(measure(theirs)),) if theirs is not None else cells)
    row("Return", lambda s: float(s.iloc[-1]))
    row("Per year", performance.annualised)
    row("Worst fall", lambda s: None if performance.max_drawdown(s) is None else min(0., performance.max_drawdown(s)) - 0.)
    lead = ""
    if theirs is not None:
        gap = float(mine.iloc[-1] - theirs.iloc[-1])
        lead = f"<b>{gap * 100:+.1f} points</b> {'ahead of' if gap >= 0 else 'behind'} {esc(name)}"
    lines = [f"{title} · {esc(_range_words(range_key, mine.index[0]))}", lead, _pre(_lines(rows)),
             "<i>Time-weighted: each day's gain on what was invested that day, so deposits never count as gains. "
             "Per year only after a full year.</i>", *_footer(account)]
    return "\n".join(line for line in lines if line), charts.returns(mine, theirs, range_key, name)


def mix_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    if mix is None:
        return "🥧 <b>Mix</b>\nNo holdings or prices to weigh.\n" + "\n".join(_footer(account)), None
    value = {row["symbol"]: row["value"] for row in mix["rows"]}
    holdings = [("", "Qty", "Value", "Gain")]
    for symbol, held in sorted(account.holdings.items(), key=lambda item: -value.get(item[0], 0)):
        price = account.prices.get(symbol)
        local = float(held["quantity"]) * price if price is not None else None
        gain = local / float(held["cost"]) - 1 if local and held["cost"] else None
        holdings.append((symbol, f"{float(held['quantity']):,.0f}", f"{value.get(symbol, 0):,.0f}", "" if gain is None else _pct(gain)))
    targeted = bool(account.targets)
    weights = [("", "Now", "Target", "Gap €") if targeted else ("", "Now")]
    for row in mix["rows"]:
        name = "Cash" if row["symbol"] == instruments.CASH else row["symbol"]
        cells = (name, f"{row['weight']:.0%}")
        weights.append(cells + (f"{row['target']:.0%}", f"{row['gap']:+,.0f}") if targeted else cells)
    lines = ["🥧 <b>Mix</b> · " + _euros(mix["total"]) + (f" · new cash → <b>{esc(mix['destination'])}</b>" if mix["destination"] else ""),
             _pre(_lines(holdings) + [""] + _lines(weights))]
    if targeted:
        lines.append("<i>Gain: against IBKR's cost basis, in the listing's currency. Gap: euros above (+) or below (−) target.</i>")
    else:
        lines.append("🎯 Set a target: /target " + " ".join(f"{s} {100 // max(1, len(account.holdings))}" for s in account.holdings))
    lines += _footer(account)
    return "\n".join(lines), charts.mix(mix["rows"])


def _euro_closes(account: Account) -> dict:
    result = {}
    for symbol, series in account.closes.items():
        currency = account.holdings.get(symbol, {}).get("currency") or next((i.currency for i in instruments.all() if i.symbol == symbol), None)
        converted = performance.in_euros(series, currency, account.eurusd_history) if currency else None
        if converted is not None:
            result[symbol] = converted
    return result


def risk_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    title = "🧩 <b>Risk</b>"
    values = {row["symbol"]: row["value"] for row in (mix or {}).get("rows", []) if row["symbol"] != instruments.CASH}
    result = performance.risk(_euro_closes(account), values) if values else None
    if result is None:
        return title + "\nNot enough shared price history to measure risk.\n" + "\n".join(_footer(account)), None
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
    lines = [f"{title} · {bets:.1f} effective bets of {len(symbols)}" if bets else title,
             _pre(_lines(rows) + (["", "Correlation", *_lines(table)] if table else []))]
    lines += ["<i>Risk: each holding's share of the portfolio's swings in euros. Effective bets: how many equal, "
              f"unrelated holdings would spread risk as evenly. Last {result['sessions']} sessions.</i>", *_footer(account)]
    return "\n".join(lines), charts.risk(result)


def entries(account: Account) -> tuple[str, bytes | None]:
    card = plan.scorecard(account.trades, account.closes)
    if not card["rows"]:
        return "🎯 <b>Entries</b>\nNo purchases in the IBKR statements yet.\n" + "\n".join(_footer(account)), None
    summary = card["summary"]
    rows = [("", "", "Paid", "vs month")]
    running = False
    for row in card["rows"][-_ENTRY_ROWS:]:
        mark = "" if row["complete"] or row["diff"] is None else "*"
        running = running or bool(mark)
        rows.append((f"{pd.Timestamp(row['date']):%d %b %y}", row["symbol"], f"{row['price']:,.2f}",
                     "—" if row["diff"] is None else f"{row['diff']:+.1%}{mark}"))
    totals = [("Buys", str(card["count"]))]
    if summary:
        totals += [("Avg vs month", f"{summary['mean']:+.2%}"), ("Chance range", f"±{summary['chance']:.2%}"),
                   ("Below avg", f"{summary['below']} of {summary['count']}")]
    verdict = {"better than chance": "✅ better than chance", "worse than chance": "❌ worse than chance",
               "within chance": "➖ within chance"}[summary["verdict"]] if summary else "no finished month yet"
    lines = [f"🎯 <b>Entries</b> · {esc(verdict)}",
             _pre(_lines(rows) + (["* month still running"] if running else []) + [""] + _lines(totals)),
             "<i>vs month: your price against that month's average close; below 0 you paid less than a buyer who did not look. "
             "Inside the chance range, timing made no measurable difference.</i>", *_footer(account)]
    noises = [row["noise"] for row in card["rows"] if row.get("noise")]
    return "\n".join(lines), charts.timing(card["rows"], sum(noises) / len(noises) if noises else None)


def _fee_rows(account: Account) -> list[dict]:
    rows = []
    for trade in account.trades:
        quantity, price = float(trade["quantity"]), float(trade["price"])
        if quantity <= 0:
            continue
        rate = account.eurusd
        if account.eurusd_history is not None and len(account.eurusd_history):
            before = account.eurusd_history[account.eurusd_history.index <= pd.Timestamp(trade["trade_date"])]
            rate = float(before.iloc[-1]) if len(before) else rate
        commission = plan.to_euros(float(trade["commission"]), trade.get("commission_currency", trade["currency"]), rate)
        value = plan.to_euros(quantity * price, trade["currency"], rate)
        if commission is None or not value:
            continue
        rows.append({"date": trade["trade_date"], "symbol": trade["symbol"], "commission_eur": commission, "value_eur": value,
                     "share": commission / value})
    return rows


def fees_tab(account: Account) -> tuple[str, bytes | None]:
    rows = _fee_rows(account)
    if not rows:
        return "🧾 <b>Fees</b>\nNo purchases in the IBKR statements yet.\n" + "\n".join(_footer(account)), None
    paid = sum(row["commission_eur"] for row in rows)
    bought = sum(row["value_eur"] for row in rows)
    last = rows[-1]
    table = [("Commissions", f"€{paid:,.2f}"), ("Of money invested", f"{paid / bought:.2%}"), ("Orders", str(len(rows))),
             ("Average order", _euros(bought / len(rows))), ("Avg commission", f"€{paid / len(rows):,.2f}"),
             (), (f"Last · {last['symbol']} {pd.Timestamp(last['date']):%-d %b}", f"€{last['commission_eur']:,.2f}"),
             ("of that order", f"{last['share']:.2%}")]
    lines = [f"🧾 <b>Fees</b> · €{paid:,.2f} on {_euros(bought)} bought", _pre(_lines(table)),
             "<i>IBKR commissions on purchases, in euros at each day's rate. Currency conversion is charged "
             "separately and is not in the trades.</i>", *_footer(account)]
    return "\n".join(lines), charts.fees(rows)


def cash_tab(account: Account, mix: dict | None) -> tuple[str, bytes | None]:
    rows = [(currency, f"{float(amount):,.2f}") for currency, amount in account.cash.items()]
    total = sum(plan.to_euros(float(a), c, account.eurusd) or 0 for c, a in account.cash.items())
    lines = [f"💶 <b>Cash</b> · {_euros(total)}"]
    if rows:
        lines.append(_pre(_lines(rows)))
    reserve = account.targets.get(instruments.CASH)
    if reserve and mix:
        keep = reserve * mix["total"]
        lines.append(f"Target {reserve:.0%} keeps {_euros(keep)} · <b>{_euros(max(0, total - keep))}</b> to invest → /whattobuy")
    recent = list(account.deposits)[-4:]
    if recent:
        lines.append(_pre(_lines([("Last paid in",), *[(f"{day:%-d %b %y}", _signed(float(amount))) for day, amount in reversed(recent)]])))
    lines += _footer(account)
    balance = None
    if len(account.nav) > 1:
        balance = pd.Series([cash for _, cash, _ in account.nav], index=pd.to_datetime([day for day, _, _ in account.nav]), dtype=float)
    return "\n".join(lines), charts.cash(balance)


def view_name(base: str, range_key: str) -> str:
    return base if range_key == DEFAULT_RANGE else f"{base}@{range_key}"


def build(account: Account) -> dict:
    """{view: (caption, png or None)} for every tab; Overview and Returns have one view per range."""
    base = account.meta.get("base_currency")
    if base and base != plan.REPORT_CURRENCY:
        account.notes.append(f"IBKR account values are in {base}, shown with a € sign")
    mix = plan.allocation(account.holdings, account.prices, account.eurusd, account.targets, account.cash)
    if mix is None and account.holdings:
        account.notes.append("a holding cannot be valued: a price or the EUR/USD rate is missing")
    past = history(account)
    views = {}
    for range_key in PORTFOLIO_RANGES:
        views[view_name("pf_overview", range_key)] = overview(account, past, range_key, mix)
        views[view_name("pf_returns", range_key)] = returns(account, past, range_key)
    views.update({"pf_mix": mix_tab(account, mix), "pf_risk": risk_tab(account, mix), "pf_entries": entries(account),
                  "pf_fees": fees_tab(account), "pf_cash": cash_tab(account, mix),
                  "pf_pe": (account.valuations or "📊 <b>P/E</b>\nValuations unavailable.", account.valuation_png)})
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
