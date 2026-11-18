"""PNG charts for the /portfolio deck, in the signal charts' visual grammar; no network or file reads.

Blue is the account, orange the benchmark, grey cash or money paid in; green marks what went the
owner's way and red what did not. Every line ends in a labelled value and listings are named on
the chart, so no reading depends on colour alone.
"""
from __future__ import annotations

from io import BytesIO

import pandas as pd

from buysignal.analysis import signal_charts as style
from buysignal.analysis.signal_charts import RANGES
from buysignal.config import TELEGRAM_CHART_DPI
from buysignal.resources import HEAVY_WORK_LOCK

CASH = "CASH"
_SPANS = {"3m": pd.DateOffset(months=3), "1y": pd.DateOffset(years=1), "5y": pd.DateOffset(years=5)}


def window(series: pd.Series, range_key: str) -> pd.Series:
    """The part of a dated series inside the range, by calendar: IBKR may skip days."""
    span = _SPANS.get(range_key)
    return series if span is None else series[series.index >= series.index[-1] - span]


def _figure(height: float):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    figure = Figure(figsize=(6, height), facecolor=style._SURFACE)
    FigureCanvasAgg(figure)
    return figure


def _png(figure) -> bytes:
    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=TELEGRAM_CHART_DPI, facecolor=style._SURFACE)
    figure.clear()
    figure.set_canvas(None)
    return buffer.getvalue()


def _bars(ax, labels, values, colors):
    positions = range(len(labels))[::-1]
    ax.barh(list(positions), values, height=.62, color=colors, edgecolor=style._SURFACE, linewidth=2, zorder=3)
    ax.set_yticks(list(positions), labels, fontsize=style._LABEL, color=style._INK)
    ax.tick_params(axis="y", length=0, pad=8)
    ax.grid(axis="x", color=style._GRID, linewidth=1, zorder=0)
    ax.grid(axis="y", visible=False)
    ax.spines[["top", "right", "bottom"]].set_visible(False)
    ax.spines["left"].set_color(style._AXIS)
    ax.tick_params(axis="x", colors=style._MUTED, labelsize=style._TICK, length=0)
    return list(positions)


def mix(rows: list[dict]) -> bytes:
    """Each listing's share of the account now as a bar, its target as a dark tick."""
    from matplotlib.ticker import FixedLocator, FuncFormatter
    with HEAVY_WORK_LOCK:
        figure = _figure(1.6 + .62 * len(rows))
        style._place(figure, left=.17, right=.95, top_points=74, bottom_points=30)
        ax = figure.subplots()
        ax.set_facecolor(style._SURFACE)
        targeted = any(row.get("target") is not None for row in rows)
        style._title(figure, "Mix", "share of the account · bar = now" + (" · tick = target" if targeted else ""))
        colors = [style._NEUTRAL if row["symbol"] == CASH else style._BLUE for row in rows]
        weights = [row["weight"] * 100 for row in rows]
        positions = _bars(ax, [row["symbol"].title() if row["symbol"] == CASH else row["symbol"] for row in rows], weights, colors)
        top = max(weights + [(row.get("target") or 0) * 100 for row in rows])
        for y, row, weight in zip(positions, rows, weights):
            target = row.get("target")
            text = f"{weight:.0f}%"
            if target is not None:
                ax.plot([target * 100] * 2, [y - .4, y + .4], color=style._INK, linewidth=2.4, solid_capstyle="butt", zorder=5)
                text += f" → {target * 100:.0f}%"
            ax.annotate(text, (max(weight, (target or 0) * 100), y), xytext=(7, 0), textcoords="offset points", va="center",
                        fontsize=style._LABEL, color=style._INK, annotation_clip=False)
        ax.set_xlim(0, top * 1.55 or 1)  # room for "now → target" after the longer of the two
        ax.yaxis.set_major_locator(FixedLocator(positions))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
        return _png(figure)


def cash(balance: pd.Series) -> bytes | None:
    """Cash in the account over time, in the base currency."""
    if balance is None or len(balance) < 2:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(4.6)
        style._place(figure, right=.80)
        ax = figure.subplots()
        style._frame(ax, "Cash", f"since {balance.index[0]:%b %Y} · daily")
        ax.fill_between(balance.index, balance.to_numpy(), 0, color=style._MUTED, alpha=.18, linewidth=0, step="post", zorder=2)
        ax.plot(balance.index, balance, color=style._SECOND, linewidth=1.8, drawstyle="steps-post", zorder=3)
        ax.set_ylim(bottom=min(0, balance.min()))
        style._yticks(ax, 4, "{:,.0f}")
        style._end(ax, balance.index[-1], balance.iloc[-1], f"€{balance.iloc[-1]:,.0f}", color=style._SECOND)
        style._dates(ax)
        return _png(figure)


def _span_words(series: pd.Series, range_key: str) -> str:
    return RANGES[range_key]["words"] or f"since {series.index[0]:%b %Y}"


def _end_label(ax, series: pd.Series, text: str, color, dy: float = 0):
    style._end(ax, series.index[-1], series.iloc[-1], text, color=color, dy=dy)


def _apart(a: float, b: float, span: float) -> tuple[float, float]:
    """Vertical nudges, in points, that keep two end labels from overlapping."""
    if span <= 0 or abs(a - b) / span > .08:
        return 0, 0
    return (9, -9) if a >= b else (-9, 9)


def overview(value: pd.Series, paid: pd.Series | None, benchmark: pd.Series | None, range_key: str, name: str) -> bytes | None:
    """Money view: the account, what was paid in, and the same deposits put into the benchmark."""
    if value is None or len(value) < 2:
        return None
    with HEAVY_WORK_LOCK:
        shown = window(value, range_key)
        figure = _figure(5.6)
        style._place(figure, right=.72)
        ax = figure.subplots()
        subtitle = _span_words(shown, range_key) + " · grey = paid in" + (f" · orange = {name}" if benchmark is not None else "")
        style._frame(ax, "Account value", subtitle)
        span = float(shown.max() - shown.min()) or 1.
        if paid is not None:
            paid = paid.reindex(shown.index)
            ax.plot(paid.index, paid, color=style._MUTED, linewidth=1.8, drawstyle="steps-post", zorder=3)
            ax.fill_between(shown.index, shown.to_numpy(), paid.to_numpy(), where=(shown >= paid).to_numpy(), interpolate=True,
                            color=style._MET, alpha=.16, linewidth=0, zorder=2)
            ax.fill_between(shown.index, shown.to_numpy(), paid.to_numpy(), where=(shown < paid).to_numpy(), interpolate=True,
                            color=style._RED, alpha=.16, linewidth=0, zorder=2)
        dy_value, dy_bench = 0, 0
        if benchmark is not None:
            bench = benchmark.reindex(shown.index).dropna()
            if len(bench) > 1:
                ax.plot(bench.index, bench, color=style._ORANGE, linewidth=1.8, zorder=3)
                dy_value, dy_bench = _apart(shown.iloc[-1], bench.iloc[-1], span)
                _end_label(ax, bench, f"{name} €{bench.iloc[-1]:,.0f}", style._ORANGE, dy_bench)
        ax.plot(shown.index, shown, color=style._BLUE, linewidth=2.4, solid_capstyle="round", zorder=4)
        _end_label(ax, shown, ("You " if benchmark is not None else "") + f"€{shown.iloc[-1]:,.0f}", style._BLUE, dy_value)
        style._yticks(ax, 5, "{:,.0f}")
        style._dates(ax)
        return _png(figure)


def returns(account: pd.Series, benchmark: pd.Series | None, range_key: str, name: str) -> bytes | None:
    """Deposits taken out: the account's time-weighted return against the benchmark's, from the range's start."""
    if account is None or len(account) < 2:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(5.4)
        style._place(figure, right=.80)
        ax = figure.subplots()
        style._frame(ax, "Return without deposits", _span_words(account, range_key) + f" · blue = you · orange = {name}")
        mine = account * 100
        span = float(mine.max() - mine.min()) or 1.
        ax.axhline(0, color=style._AXIS, linewidth=1, zorder=2)
        dy_mine = 0
        if benchmark is not None and len(benchmark) > 1:
            theirs = benchmark * 100
            ax.plot(theirs.index, theirs, color=style._ORANGE, linewidth=1.8, zorder=3)
            dy_mine, dy_theirs = _apart(mine.iloc[-1], theirs.iloc[-1], span)
            _end_label(ax, theirs, f"{theirs.iloc[-1]:+.1f}%", style._ORANGE, dy_theirs)
        ax.plot(mine.index, mine, color=style._BLUE, linewidth=2.4, zorder=4)
        _end_label(ax, mine, f"{mine.iloc[-1]:+.1f}%", style._BLUE, dy_mine)
        style._yticks(ax, 5, "{:+.0f}%")
        style._zero(ax, "0%")
        style._dates(ax)
        return _png(figure)


def risk(result: dict) -> bytes:
    """Share of the money beside share of the risk, per holding: a holding whose risk share is
    much larger than its money share is where the portfolio's swings come from."""
    from matplotlib.ticker import FuncFormatter
    with HEAVY_WORK_LOCK:
        symbols = result["symbols"]
        figure = _figure(1.9 + .85 * len(symbols))
        style._place(figure, left=.17, right=.95, top_points=74, bottom_points=30)
        ax = figure.subplots()
        ax.set_facecolor(style._SURFACE)
        style._title(figure, "Where the risk is", "grey = share of money · blue = share of swings")
        positions = list(range(len(symbols)))[::-1]
        money = [result["weights"][s] * 100 for s in symbols]
        swings = [result["risk"][s] * 100 for s in symbols]
        ax.barh([p + .19 for p in positions], money, height=.36, color=style._NEUTRAL, edgecolor=style._SURFACE, linewidth=2, zorder=3)
        ax.barh([p - .19 for p in positions], swings, height=.36, color=style._BLUE, edgecolor=style._SURFACE, linewidth=2, zorder=3)
        for p, m, r in zip(positions, money, swings):
            ax.annotate(f"{m:.0f}%", (m, p + .19), xytext=(5, 0), textcoords="offset points", va="center", fontsize=style._NOTE, color=style._SECOND)
            ax.annotate(f"{r:.0f}%", (max(r, 0), p - .19), xytext=(5, 0), textcoords="offset points", va="center",
                        fontsize=style._NOTE, color=style._INK, fontweight="bold")
        ax.set_yticks(positions, symbols, fontsize=style._LABEL, color=style._INK)
        ax.tick_params(axis="y", length=0, pad=8)
        ax.tick_params(axis="x", colors=style._MUTED, labelsize=style._TICK, length=0)
        ax.grid(axis="x", color=style._GRID, linewidth=1, zorder=0)
        ax.spines[["top", "right", "bottom"]].set_visible(False)
        ax.spines["left"].set_color(style._AXIS)
        ax.set_xlim(min(0, min(swings) * 1.2), max(money + swings) * 1.25)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
        return _png(figure)


def timing(rows: list[dict], noise: float | None) -> bytes | None:
    """Each purchase's price against its month's average: below zero you paid less than a buyer
    who did not look. The grey band is how far one day usually sits from its month's average."""
    judged = [row for row in rows if row.get("diff") is not None]
    if not judged:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(5.0)
        style._place(figure, right=.95, bottom_points=46)
        ax = figure.subplots()
        mean = sum(row["diff"] for row in judged) / len(judged) * 100
        style._frame(ax, "Entry timing", f"vs the month's average · mean {mean:+.1f}%" + (" · grey = usual day" if noise else ""))
        diffs = [row["diff"] * 100 for row in judged]
        x = list(range(len(judged)))
        if noise:
            ax.axhspan(-noise * 100, noise * 100, color=style._MUTED, alpha=.14, linewidth=0, zorder=1)
        colors = [style._MET if d < 0 else style._RED for d in diffs]
        ax.bar(x, diffs, width=.62, color=colors, edgecolor=style._SURFACE, linewidth=2, zorder=3)
        ax.axhline(0, color=style._AXIS, linewidth=1, zorder=2)
        ax.set_xticks(x, [f"{row['symbol']}\n{pd.Timestamp(row['date']):%b %y}" for row in judged], fontsize=style._NOTE)
        ax.tick_params(axis="x", colors=style._SECOND, length=0)
        ax.axhline(mean, color=style._INK, linewidth=1.4, linestyle=(0, (5, 4)), zorder=4)
        style._yticks(ax, 5, "{:+.0f}%")
        style._zero(ax, "0%")
        if len(judged) > 10:
            ax.set_xticks(x[::max(1, len(x) // 8)])
        return _png(figure)


def fees(trades: list[dict]) -> bytes | None:
    """Commissions paid: running total in euros, and each order's commission as a share of it."""
    if not trades:
        return None
    with HEAVY_WORK_LOCK:
        frame = pd.DataFrame(trades).sort_values("date")
        figure = _figure(6.2)
        style._place(figure, right=.82, gap=.5, top_points=104)
        top, bottom = figure.subplots(2, 1, gridspec_kw={"height_ratios": [1, 1]})
        style._frame(top, "Fees", f"{len(frame)} orders since {pd.Timestamp(frame.date.iloc[0]):%b %Y}")
        style._heading(top, "Commissions paid, running total €")
        total = frame.set_index(pd.to_datetime(frame.date)).commission_eur.cumsum()
        top.plot(total.index, total, color=style._BLUE, linewidth=2.2, drawstyle="steps-post", zorder=4)
        style._end(top, total.index[-1], total.iloc[-1], f"€{total.iloc[-1]:,.2f}")
        style._yticks(top, 4, "{:,.0f}")
        style._dates(top)
        style._frame(bottom)
        style._heading(bottom, "Each order's commission, % of its value")
        x = list(range(len(frame)))
        bottom.bar(x, frame.share * 100, width=.62, color=style._BLUE, edgecolor=style._SURFACE, linewidth=2, zorder=3)
        bottom.set_xticks(x, [f"{pd.Timestamp(d):%b %y}" for d in frame.date], fontsize=style._NOTE)
        bottom.tick_params(axis="x", colors=style._SECOND, length=0)
        if len(x) > 10:
            bottom.set_xticks(x[::max(1, len(x) // 8)])
        style._yticks(bottom, 4, "{:.2f}%")
        return _png(figure)
