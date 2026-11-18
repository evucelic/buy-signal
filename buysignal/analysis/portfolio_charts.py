"""PNG charts for the /portfolio deck: blue is the account, orange the S&P 500, violet the target mix,
dark grey the world index.
"""
from __future__ import annotations

import pandas as pd

from buysignal.analysis import signal_charts as style
from buysignal.resources import HEAVY_WORK_LOCK

CASH = "CASH"
_VIOLET = "#7d5bc6"
LINES = {"You": style._BLUE, "S&P 500": style._ORANGE, "Target mix": _VIOLET, "All-World": style._SECOND}
SHORT_NAMES = {"S&P 500": "S&P", "Target mix": "Mix", "All-World": "World", "You": "You"}
# Listings in the mix, in the order they are drawn; cash is always light grey.
_SLICES = (style._BLUE, style._ORANGE, style._MET, _VIOLET, "#d9a521", "#3fa7b5", style._RED)
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
    return style.png_bytes(figure)


def _spread(ends: dict[str, float], span: float, gap: float = .075) -> dict[str, float]:
    """Vertical nudges, in points, that keep end labels at least `gap` of the axis apart."""
    if span <= 0:
        return {name: 0. for name in ends}
    order = sorted(ends, key=ends.get)
    placed, result = [], {}
    for name in order:
        y = ends[name]
        if placed and y - placed[-1] < gap * span:
            y = placed[-1] + gap * span
        placed.append(y)
        result[name] = (y - ends[name]) / span * 260  # about 260 points of plot height
    return result


def _labelled_lines(ax, lines: dict[str, pd.Series], fmt: str):
    span = float(max(s.max() for s in lines.values()) - min(s.min() for s in lines.values())) or 1.
    nudges = _spread({name: float(s.iloc[-1]) for name, s in lines.items()}, span)
    for name, series in lines.items():
        color = LINES.get(name, style._SECOND)
        you = name == "You"
        ax.plot(series.index, series, color=color, linewidth=2.6 if you else 1.7, zorder=5 if you else 3,
                linestyle=(0, (4, 2)) if name == "All-World" else "-")
        x, y = series.index[-1], series.iloc[-1]
        ax.scatter([x], [y], s=46 if you else 30, color=color, edgecolors=style._SURFACE, linewidths=1.5, zorder=6)
        ax.annotate(f"{SHORT_NAMES.get(name, name)} {fmt.format(y)}", (x, y), xytext=(8, nudges[name]), textcoords="offset points",
                    va="center", ha="left", fontsize=style._LABEL, fontweight="bold", color=color, annotation_clip=False)


def value(account: pd.Series, paid: pd.Series, benchmarks: dict[str, pd.Series], mwr: str | None = None) -> bytes | None:
    if account is None or len(account) < 2:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(5.8)
        style._place(figure, right=.72)
        ax = figure.subplots()
        style._frame(ax, "Account value", "grey = paid in")
        if mwr:
            ax.text(.02, .97, f"{mwr}\nmoney-weighted", transform=ax.transAxes, va="top", ha="left", fontsize=style._LABEL,
                    fontweight="bold", color=style._BLUE, linespacing=1.3)
        paid = paid.reindex(account.index)
        ax.plot(paid.index, paid, color=style._MUTED, linewidth=1.6, drawstyle="steps-post", zorder=2)
        ax.fill_between(account.index, account.to_numpy(), paid.to_numpy(), where=(account >= paid).to_numpy(), interpolate=True,
                        color=style._MET, alpha=.14, linewidth=0, zorder=1)
        ax.fill_between(account.index, account.to_numpy(), paid.to_numpy(), where=(account < paid).to_numpy(), interpolate=True,
                        color=style._RED, alpha=.14, linewidth=0, zorder=1)
        lines = {name: series.reindex(account.index).dropna() for name, series in benchmarks.items()}
        lines = {name: series for name, series in lines.items() if len(series) > 1}
        _labelled_lines(ax, {**lines, "You": account}, "€{:,.0f}")
        style._yticks(ax, 5, "{:,.0f}")
        style._dates(ax)
        return _png(figure)


def returns(cumulative: dict[str, pd.Series]) -> bytes | None:
    """Time-weighted returns, and below them the account's fall from its previous high."""
    mine = cumulative.get("You")
    if mine is None or len(mine) < 2:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(6.6)
        style._place(figure, right=.74, gap=.42, top_points=74)
        top, bottom = figure.subplots(2, 1, gridspec_kw={"height_ratios": [2.2, 1]}, sharex=True)
        style._frame(top, "Time-weighted return (TWR)", "deposits removed · every day weighs the same")
        top.axhline(0, color=style._AXIS, linewidth=1, zorder=2)
        _labelled_lines(top, {name: series * 100 for name, series in cumulative.items() if len(series) > 1}, "{:+.1f}%")
        style._yticks(top, 5, "{:+.0f}%")
        style._zero(top, "0%")
        style._frame(bottom)
        style._heading(bottom, "Fall from high · orange = S&P")
        for name in ("S&P 500", "You"):
            series = cumulative.get(name)
            if series is None or len(series) < 2:
                continue
            fall = ((1 + series) / (1 + series).cummax() - 1) * 100
            if name == "You":
                bottom.fill_between(fall.index, fall.to_numpy(), 0, color=style._RED, alpha=.18, linewidth=0, zorder=2)
                bottom.plot(fall.index, fall, color=style._BLUE, linewidth=1.8, zorder=4)
                style._end(bottom, fall.index[-1], fall.iloc[-1], f"{fall.iloc[-1]:.1f}%")
            else:
                bottom.plot(fall.index, fall, color=style._ORANGE, linewidth=1.2, zorder=3)
        bottom.set_ylim(top=0)
        style._yticks(bottom, 3, "{:.0f}%")
        style._zero(bottom, "0%")
        style._dates(bottom)
        return _png(figure)


def mix(rows: list[dict], total: float) -> bytes:
    """Two rings: now outside, target inside."""
    with HEAVY_WORK_LOCK:
        figure = _figure(4.6)
        targeted = any(row.get("target") is not None for row in rows)
        style._title(figure, "Mix", "outer now" + (" · inner target" if targeted else ""))
        ax = figure.add_axes((.0, .02, .58, .80))
        ax.set_aspect("equal")
        ax.axis("off")
        colors, listed = [], 0
        for row in rows:
            if row["symbol"] == CASH:
                colors.append(style._NEUTRAL)
            else:
                colors.append(_SLICES[listed % len(_SLICES)])
                listed += 1
        now = [max(row["weight"], 0) for row in rows]
        edge = {"edgecolor": style._SURFACE, "linewidth": 2}
        ax.pie(now, radius=1, colors=colors, startangle=90, counterclock=False, wedgeprops={"width": .28, **edge})
        if targeted:
            ax.pie([row.get("target") or 0 for row in rows], radius=.68, colors=colors, startangle=90, counterclock=False,
                   wedgeprops={"width": .2, "alpha": .55, **edge})
        ax.text(0, 0, f"€{total:,.0f}", ha="center", va="center", fontsize=style._LABEL + 1, fontweight="bold", color=style._INK)
        height = figure.get_figheight() * 72
        for i, (row, color) in enumerate(zip(rows, colors)):
            y = 1 - (100 + i * 34) / height
            figure.patches.append(_swatch(figure, .60, y, color))
            name = "Cash" if row["symbol"] == CASH else row["symbol"]
            text = f"{name:<5} {row['weight']:>4.0%}"
            if targeted:
                text += f"  → {(row.get('target') or 0):.0%}"
            figure.text(.65, y, text, fontsize=style._LABEL, color=style._INK, va="center", family="monospace")
        return _png(figure)


def _swatch(figure, x, y, color):
    from matplotlib.patches import Rectangle
    return Rectangle((x, y - .012), .03, .024, transform=figure.transFigure, color=color, figure=figure)


def risk(result: dict) -> bytes:
    from matplotlib.ticker import FuncFormatter
    with HEAVY_WORK_LOCK:
        symbols = result["symbols"]
        figure = _figure(1.9 + .85 * len(symbols))
        style._place(figure, left=.17, right=.95, top_points=74, bottom_points=30)
        ax = figure.subplots()
        ax.set_facecolor(style._SURFACE)
        style._title(figure, "Where the risk is", "grey money · blue swings")
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
    """Each purchase against its month's average; the grey band is a usual day's distance from it."""
    judged = [row for row in rows if row.get("diff") is not None]
    if not judged:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(5.0)
        style._place(figure, right=.95, bottom_points=46)
        ax = figure.subplots()
        mean = sum(row["diff"] for row in judged) / len(judged) * 100
        style._frame(ax, "Entry timing", f"vs month average · mean {mean:+.1f}%")
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


def cash(deposits: pd.Series, balance: pd.Series | None) -> bytes | None:
    if deposits is None or deposits.empty:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(6.2)
        style._place(figure, right=.80, gap=.5, top_points=74)
        top, bottom = figure.subplots(2, 1, gridspec_kw={"height_ratios": [1.4, 1]})
        style._frame(top, "Deposits", "grey = uninvested cash")
        total = deposits.cumsum()
        start = deposits.index[0] - pd.Timedelta(days=1)
        steps = pd.concat([pd.Series([0.], index=[start]), total])
        if balance is not None and len(balance) > 1:
            shown = balance[balance.index >= start]
            top.fill_between(shown.index, shown.to_numpy(), 0, color=style._MUTED, alpha=.3, linewidth=0, step="post", zorder=2)
        top.plot(steps.index, steps, color=style._BLUE, linewidth=2.4, drawstyle="steps-post", zorder=4)
        top.scatter(total.index, total, s=22, color=style._BLUE, edgecolors=style._SURFACE, linewidths=1.2, zorder=5)
        style._end(top, total.index[-1], total.iloc[-1], f"€{total.iloc[-1]:,.0f}")
        top.set_ylim(bottom=0)
        style._yticks(top, 4, "{:,.0f}")
        style._dates(top)
        style._frame(bottom)
        style._heading(bottom, "Paid in per month, €")
        monthly = deposits.groupby(deposits.index.to_period("M")).sum()
        months = pd.period_range(monthly.index[0], deposits.index[-1].to_period("M"), freq="M")
        monthly = monthly.reindex(months, fill_value=0.)
        x = list(range(len(monthly)))
        bottom.bar(x, monthly.to_numpy(), width=.7, color=[style._BLUE if v >= 0 else style._RED for v in monthly], zorder=3)
        step = -(-len(x) // 5)
        bottom.set_xticks(x[::step], [p.strftime("%b %y") for p in monthly.index[::step]], fontsize=style._NOTE)
        bottom.tick_params(axis="x", colors=style._SECOND, length=0)
        style._yticks(bottom, 3, "{:,.0f}")
        return _png(figure)


def tax(schedule: list[tuple], lots: list, today) -> bytes | None:
    """Tax on a full sale at today's prices on each coming date, and each purchase's gain."""
    if not lots:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(6.2)
        style._place(figure, right=.80, gap=.55, top_points=74, bottom_points=40)
        top, bottom = figure.subplots(2, 1, gridspec_kw={"height_ratios": [1, 1]})
        style._frame(top, "Tax if you sold everything", "at today's prices")
        days = [pd.Timestamp(day) for day, _ in schedule]
        amounts = [amount for _, amount in schedule]
        end = max(days[-1] + pd.Timedelta(days=45), pd.Timestamp(today) + pd.Timedelta(days=60))
        top.step(days + [end], amounts + [amounts[-1]], where="post", color=style._ORANGE, linewidth=2.4, zorder=4)
        top.fill_between(days + [end], amounts + [amounts[-1]], 0, step="post", color=style._ORANGE, alpha=.12, linewidth=0, zorder=2)
        top.scatter([days[0]], [amounts[0]], s=46, color=style._ORANGE, edgecolors=style._SURFACE, linewidths=1.5, zorder=6)
        top.annotate(f"€{amounts[0]:,.0f} today", (days[0], amounts[0]), xytext=(2, 10), textcoords="offset points", va="bottom",
                     fontsize=style._LABEL, fontweight="bold", color=style._INK, path_effects=style._halo())
        top.set_ylim(bottom=0, top=max(amounts) * 1.25 or 1)
        style._yticks(top, 4, "{:,.0f}")
        style._dates(top)
        style._frame(bottom)
        style._heading(bottom, "Gain per purchase · orange taxed · green free")
        dated = [lot for lot in lots if lot.bought is not None]
        for lot in dated:
            free = lot.free_from(None)
            color = style._ORANGE if free and pd.Timestamp(free) > pd.Timestamp(today) else style._MET
            bottom.bar(pd.Timestamp(lot.bought), lot.gain, width=6, color=color, zorder=3)
        bottom.axhline(0, color=style._AXIS, linewidth=1, zorder=2)
        style._yticks(bottom, 3, "{:+,.0f}")
        style._zero(bottom, "0")
        style._dates(bottom)
        return _png(figure)


def valuation(history: pd.DataFrame, summary: dict) -> bytes | None:
    """CAPE since 1881; each month's excess yield against the next ten years' real return."""
    if history is None or history.empty:
        return None
    with HEAVY_WORK_LOCK:
        figure = _figure(6.8)
        style._place(figure, right=.80, gap=.62, top_points=74, bottom_points=46)
        top, bottom = figure.subplots(2, 1, gridspec_kw={"height_ratios": [1, 1.15]})
        style._frame(top, "US stocks: CAPE", f"since {summary['since']:%Y}")
        cape = history.set_index("date").cape.dropna()
        top.plot(cape.index, cape, color=style._BLUE, linewidth=1.4, zorder=3)
        top.axhline(summary["cape_median"], color=style._SECOND, linewidth=1.2, linestyle=(0, (5, 4)), zorder=2)
        style._end(top, cape.index[-1], cape.iloc[-1], f"{cape.iloc[-1]:.1f}")
        top.annotate(f"median {summary['cape_median']:.0f}", (0, summary["cape_median"]), xycoords=("axes fraction", "data"),
                     xytext=(4, 4), textcoords="offset points", fontsize=style._NOTE, color=style._SECOND, path_effects=style._halo())
        style._yticks(top, 4, "{:.0f}")
        style._dates(top)
        style._frame(bottom)
        style._heading(bottom, "Next 10 years' real return vs excess yield then")
        done = history.dropna(subset=["excess_yield", "stocks_10y"])
        near = (done.excess_yield - summary["excess_yield"]).abs() <= summary["band"]
        bottom.scatter(done.excess_yield[~near] * 100, done.stocks_10y[~near] * 100, s=7, color=style._NEUTRAL, linewidths=0, zorder=2)
        bottom.scatter(done.excess_yield[near] * 100, done.stocks_10y[near] * 100, s=9, color=style._BLUE, alpha=.7, linewidths=0, zorder=3)
        bottom.axvline(summary["excess_yield"] * 100, color=style._INK, linewidth=1.4, linestyle=(0, (5, 4)), zorder=4)
        bottom.annotate(f"now {summary['excess_yield']:.1%}", (summary["excess_yield"] * 100, 1), xycoords=("data", "axes fraction"),
                        xytext=(5, -4), textcoords="offset points", va="top", fontsize=style._NOTE, color=style._INK, path_effects=style._halo())
        bottom.axhline(0, color=style._AXIS, linewidth=1, zorder=1)
        bottom.set_xlabel("excess CAPE yield, %", fontsize=style._NOTE, color=style._SECOND)
        style._yticks(bottom, 4, "{:+.0f}%")
        style._zero(bottom, "0%")
        bottom.tick_params(axis="x", colors=style._MUTED, labelsize=style._TICK, length=0)
        return _png(figure)
