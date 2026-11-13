"""Snapshot-based PNG charts for the Telegram panels; no network or file reads.

One visual grammar across every chart, so a phone-sized image reads without its caption:
the measure is one blue line ending in a labelled dot, grey is "the usual range" or a neutral
reading, a dashed rule is a threshold, green marks fear or a dip, and red marks a warning state.
Titles name the measure and the visible range; everything else is a direct label.
"""

from __future__ import annotations

from io import BytesIO
from collections import OrderedDict
import hashlib
import threading
import textwrap

import numpy as np
import pandas as pd

from buysignal.config import (
    MARKET_DIP_THRESHOLD, MARKET_DRAWDOWN_SESSIONS, VIX_SOFT, VIX_STRONG, TELEGRAM_CHART_DPI,
    CHART_CACHE_MAX_BYTES, SECTOR_MIN_ESTIMATE_COVERAGE, CREDIT_STRESS_MIN_OBS,
    CREDIT_STRESS_PERCENTILE, CREDIT_STRESS_WINDOW,
)
from buysignal.resources import HEAVY_WORK_LOCK
from buysignal.signals.base import SubSignal

_LOCK = HEAVY_WORK_LOCK
_CACHE_LOCK = threading.Lock()
_CACHE = OrderedDict()
_CACHE_BYTES = 0

# Validated reference palette (dataviz skill): marks carry colour, text stays in ink tokens.
_SURFACE, _INK, _SECOND, _MUTED, _GRID, _AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
_BLUE, _ORANGE, _MET, _RED, _NEUTRAL = "#2a78d6", "#eb6834", "#1baf7a", "#e34948", "#d5d4cc"

# Text is sized for a 6-inch canvas shown about 2.6 inches wide on a phone.
_TITLE, _LABEL, _TICK, _NOTE = 18, 13.5, 12.5, 11.5
_EDGE = .035  # left edge shared by every heading, as a fraction of the canvas width

# Range buttons. Sessions apply to daily series, months to monthly ones; None means all history.
RANGES = {
    "3m": {"label": "3M", "words": "3 months", "sessions": 63, "months": None},
    "1y": {"label": "1Y", "words": "1 year", "sessions": 252, "months": 12},
    "5y": {"label": "5Y", "words": "5 years", "sessions": 1260, "months": 60},
    "max": {"label": "All", "words": None, "sessions": None, "months": None},
}
DEFAULT_RANGE = "1y"
SECTION_RANGES = {
    "market_dip": ("3m", "1y", "5y", "max"),
    "vix": ("3m", "1y", "5y", "max"),
    "credit": ("3m", "1y", "5y", "max"),
    "margin_debt": ("1y", "5y", "max"),
    "yield_curve": ("1y", "5y", "max"),
}
_MONTHLY = {"margin_debt"}


def _series_length(signal: SubSignal, index: str | None = None) -> int:
    history = signal.history
    if history is None or history.empty:
        return 0
    if signal.name == "market_dip":
        column = index or signal.metrics.get("chart_index", "SPY")
        return int(history[column].notna().sum()) if column in history else 0
    return len(history)


def available_ranges(signal: SubSignal, index: str | None = None) -> tuple[str, ...]:
    """Named ranges the history can actually fill, then "All" when it holds still more."""
    choices = SECTION_RANGES.get(signal.name, ())
    length = _series_length(signal, index)
    unit = "months" if signal.name in _MONTHLY else "sessions"
    result, shown = [], 0
    for key in choices:
        size = RANGES[key][unit]
        if size is None:
            # "All" earns a button only when it shows clearly more than the longest named range.
            if length > shown * 1.15:
                result.append(key)
        elif key == DEFAULT_RANGE or length >= size * .9:
            # The default range is always offered, so a section never loses its chart.
            result.append(key)
            shown = max(shown, min(size, length))
    return tuple(result)


def _cut(frame, range_key: str, monthly: bool = False):
    size = RANGES[range_key]["months" if monthly else "sessions"]
    return frame if size is None else frame.tail(size)


def _thin(frame: pd.DataFrame, how: dict) -> tuple[pd.DataFrame, str]:
    """Daily under ~2 years, weekly to ~9 years, monthly beyond: the frequency follows the span.

    Completed periods are summarised; the period still in progress keeps its daily rows, so the
    line always ends on the latest real observation rather than on a partial-period summary.
    """
    if len(frame) < 3:
        return frame, "daily"
    span = (frame.index[-1] - frame.index[0]).days
    if span <= 800:
        return frame, "daily"
    rule, period, name = ("W-FRI", "W-FRI", "weekly") if span <= 3300 else ("ME", "M", "monthly")
    periods = frame.index.to_period(period)
    in_progress = periods == periods[-1]
    closed = frame.loc[~in_progress].resample(rule).agg(how).dropna(how="all")
    return pd.concat([closed, frame.loc[in_progress]]), name


def _range_words(range_key: str, index: pd.DatetimeIndex, frequency: str) -> str:
    words = RANGES[range_key]["words"] or f"since {index[0].year}"
    return f"{words} · {frequency}"


def _frame(ax, title: str | None = None, subtitle: str | None = None):
    ax.set_facecolor(_SURFACE)
    ax.tick_params(colors=_MUTED, labelsize=_TICK, length=0, pad=6)
    ax.grid(axis="y", color=_GRID, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(_AXIS)
    ax.margins(x=.02)
    if title:
        _title(ax.figure, title, subtitle)


def _title(figure, title: str, subtitle: str | None = None):
    """Heading pinned to the canvas edge, so it lines up whatever the axis labels need."""
    height = figure.get_figheight() * 72
    figure.text(_EDGE, 1 - 13 / height, title, fontsize=_TITLE, fontweight="bold", color=_INK, va="top", ha="left")
    if subtitle:
        figure.text(_EDGE, 1 - 39 / height, subtitle, fontsize=_LABEL, color=_SECOND, va="top", ha="left")


def _place(figure, *, left=.115, right=.865, top_points=70, bottom_points=34, gap=.2):
    height = figure.get_figheight() * 72
    figure.subplots_adjust(left=left, right=right, top=1 - top_points / height, bottom=bottom_points / height, hspace=gap)


def _heading(ax, text: str):
    """Small label for a secondary panel; quieter than the chart title."""
    ax.set_title(text, loc="left", fontsize=_LABEL, color=_SECOND, pad=8)


def _dates(ax):
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
    locator = AutoDateLocator(minticks=3, maxticks=5)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(ConciseDateFormatter(locator, show_offset=False))


def _yticks(ax, count: int = 5, fmt: str | None = None):
    from matplotlib.ticker import MaxNLocator, FuncFormatter
    ax.yaxis.set_major_locator(MaxNLocator(nbins=count, min_n_ticks=3))
    if fmt:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: fmt.format(value)))


def _halo():
    """Surface-coloured outline that keeps a note readable where the data runs through it."""
    from matplotlib.patheffects import withStroke
    return [withStroke(linewidth=3.5, foreground=_SURFACE)]


def _zero(ax, text: str):
    """A signed axis writes its zero plainly rather than as "+0"."""
    from matplotlib.ticker import FuncFormatter
    signed = ax.yaxis.get_major_formatter()
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, position: text if abs(value) < 1e-9 else signed(value, position)))


def _end(ax, x, y, text: str, color=_BLUE, dy: float = 0):
    """Dot on the latest observation with its value: the one number the chart must give."""
    ax.scatter([x], [y], s=64, color=color, edgecolors=_SURFACE, linewidths=2, zorder=6)
    ax.annotate(text, (x, y), xytext=(9, dy), textcoords="offset points", va="center", ha="left",
                fontsize=_LABEL, fontweight="bold", color=_INK, annotation_clip=False)


def _rule(ax, y: float, text: str, above: bool = True):
    """A threshold: dashed so it cannot be mistaken for a gridline, named where it starts."""
    ax.axhline(y, color=_SECOND, linewidth=1.2, linestyle=(0, (5, 4)), zorder=3)
    ax.annotate(text, (0, y), xycoords=("axes fraction", "data"), xytext=(4, 4 if above else -4),
                textcoords="offset points", va="bottom" if above else "top", ha="left",
                fontsize=_NOTE, color=_SECOND, path_effects=_halo(), zorder=7)


def _usual(ax, low: float | None, high: float | None) -> bool:
    """Grey band for the middle half of past readings; named in the subtitle, not on the plot."""
    if low is None or high is None or not np.isfinite([low, high]).all():
        return False
    ax.axhspan(low, high, color=_MUTED, alpha=.14, linewidth=0, zorder=1)
    return True


def render_signal_chart(signal: SubSignal, range_key: str = DEFAULT_RANGE) -> bytes | None:
    global _CACHE_BYTES
    if signal.history is None or signal.history.empty:
        return None
    hashed = pd.util.hash_pandas_object(signal.history, index=True).values.tobytes()
    key = (signal.name, range_key, signal.observed_at, signal.metrics.get("chart_index"), tuple(signal.history.columns),
           hashlib.sha256(hashed).digest(), TELEGRAM_CHART_DPI)
    with _LOCK:
        with _CACHE_LOCK:
            if key in _CACHE:
                _CACHE.move_to_end(key)
                return _CACHE[key]
        png = _render_signal_chart(signal, range_key)
        if png is not None and len(png) <= CHART_CACHE_MAX_BYTES:
            with _CACHE_LOCK:
                while _CACHE and _CACHE_BYTES + len(png) > CHART_CACHE_MAX_BYTES:
                    _, evicted = _CACHE.popitem(last=False)
                    _CACHE_BYTES -= len(evicted)
                _CACHE[key] = png
                _CACHE_BYTES += len(png)
        return png


def _market(figure, signal, range_key):
    index_name = signal.metrics.get("chart_index", "SPY")
    price = signal.history[index_name].dropna()
    peak = price.shift(1).rolling(MARKET_DRAWDOWN_SESSIONS).max()
    frame = pd.DataFrame({
        "price": price, "average": price.rolling(200).mean(),
        "drawdown": (price / peak - 1).clip(upper=0) * 100,
    })
    # A thinned period keeps its deepest drawdown, so a crash is never averaged away.
    visible, frequency = _thin(_cut(frame, range_key), {"price": "last", "average": "last", "drawdown": "min"})
    top, bottom = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [1.7, 1]})
    names = {"SPY": "S&P 500 (SPY)", "NASDAQ": "Nasdaq", "DOW": "Dow Jones", "EURO50": "Euro Stoxx 50"}
    _frame(top, names.get(index_name, index_name), _range_words(range_key, visible.index, frequency))
    top.plot(visible.index, visible.price, color=_BLUE, linewidth=2.2, solid_capstyle="round", zorder=4)
    if visible.average.notna().any():
        top.plot(visible.index, visible.average, color=_MUTED, linewidth=1.6, zorder=3)
        placed = visible.average.dropna()
        # Named on the line itself; a legend box would cost a row for one grey line.
        anchor = placed.index[len(placed) // 3]
        below = placed.loc[anchor] <= visible.price.loc[anchor]
        top.annotate("200-day average", (anchor, placed.loc[anchor]), xytext=(0, -7 if below else 7),
                     textcoords="offset points", va="top" if below else "bottom", ha="center",
                     fontsize=_NOTE, color=_SECOND, path_effects=_halo(), zorder=7)
    if frequency == "monthly":
        from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
        top.set_yscale("log")  # decades of growth: equal distances are equal percentage moves
        top.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        top.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
        top.yaxis.set_minor_formatter(NullFormatter())
        top.minorticks_off()
    else:
        _yticks(top, 5, "{:,.0f}")
    last = visible.price.iloc[-1]
    _end(top, visible.index[-1], last, f"{last:,.0f}" if last >= 1000 else f"{last:,.2f}")

    _frame(bottom)
    _heading(bottom, "% below 3-month high")
    depth = visible.drawdown
    bottom.fill_between(depth.index, depth.to_numpy(), 0, color=_BLUE, alpha=.12, linewidth=0, zorder=2)
    bottom.plot(depth.index, depth, color=_BLUE, linewidth=1.6, zorder=4)
    if index_name == "SPY":
        threshold = MARKET_DIP_THRESHOLD * 100
        met = depth.to_numpy() <= threshold + 1e-9
        bottom.fill_between(depth.index, depth.to_numpy(), threshold, where=met, interpolate=True,
                            color=_MET, alpha=.45, linewidth=0, zorder=3)
        _rule(bottom, threshold, f"dip {threshold:.0f}%", above=False)
        bottom.set_ylim(top=0, bottom=min(depth.min() * 1.12, threshold * 1.9))
    else:
        bottom.set_ylim(top=0)
    _yticks(bottom, 4, "{:.0f}%")
    if pd.notna(depth.iloc[-1]):
        _end(bottom, depth.index[-1], depth.iloc[-1], f"{depth.iloc[-1]:.1f}%")
    _dates(bottom)


def _level(figure, signal, range_key, *, column, title, scale=1., unit="", thresholds=(), met_above=None,
           met_color=_MET, met_alpha=.45, floor=None, decimals=1, signed=False):
    """One measure over time against its usual range and its named thresholds."""
    history = signal.history
    series = pd.Series(history[column].to_numpy(dtype=float) * scale, index=pd.DatetimeIndex(history["date"])).dropna()
    if signal.name in _MONTHLY:
        visible, frequency = _cut(series.to_frame("value"), range_key, monthly=True), "monthly"
    else:
        visible, frequency = _thin(_cut(series.to_frame("value"), range_key), {"value": "last"})
    ax = figure.subplots()
    low, high = signal.metrics.get("usual_low"), signal.metrics.get("usual_high")
    if low is None or high is None:
        from buysignal.analysis.context import usual_range
        band = usual_range(series)
        low, high = band if band else (None, None)
    else:
        low, high = low * scale, high * scale
    banded = _usual(ax, low, high)
    _frame(ax, title, _range_words(range_key, visible.index, frequency) + (" · grey = usual range" if banded else ""))
    values = visible.value
    ax.plot(values.index, values, color=_BLUE, linewidth=2.2, solid_capstyle="round", zorder=4)
    top = values.max()
    for level, text, always in thresholds:
        # A far-away threshold would flatten the line; it is drawn once the data gets near it.
        if always or values.max() >= level * .8:
            _rule(ax, level, text)
            top = max(top, level)
    if met_above is not None:
        ax.fill_between(values.index, values.to_numpy(), met_above, where=values.to_numpy() >= met_above,
                        interpolate=True, color=met_color, alpha=met_alpha, linewidth=0, zorder=3)
    lowest = values.min() if floor is None else min(floor, values.min())
    pad = (top - lowest) * .09 or 1
    ax.set_ylim(lowest - pad * (0 if floor is not None and lowest == floor else 1), top + pad * 1.6)
    _yticks(ax, 5)
    _end(ax, values.index[-1], values.iloc[-1], f"{values.iloc[-1]:{'+' if signed else ''}.{decimals}f}{unit}")
    _dates(ax)


def _credit(figure, signal, range_key):
    """The spread against its stress line, which moves: each day's line comes from the ten years before it."""
    history = signal.history
    spread = pd.Series(history["spread"].to_numpy(dtype=float) * 100, index=pd.DatetimeIndex(history["date"])).dropna()
    line = spread.rolling(CREDIT_STRESS_WINDOW, min_periods=CREDIT_STRESS_MIN_OBS).quantile(CREDIT_STRESS_PERCENTILE)
    frame = pd.DataFrame({"value": spread, "line": line})
    visible, frequency = _thin(_cut(frame, range_key), {"value": "last", "line": "last"})
    ax = figure.subplots()
    _frame(ax, "Credit spread", _range_words(range_key, visible.index, frequency) + " · red = stressed")
    values, limit = visible.value, visible.line
    ax.plot(values.index, values, color=_BLUE, linewidth=2.2, solid_capstyle="round", zorder=4)
    drawn = limit.dropna()
    top = values.max()
    if len(drawn):
        ax.plot(drawn.index, drawn, color=_SECOND, linewidth=1.2, linestyle=(0, (5, 4)), zorder=3)
        stressed = (values >= limit).fillna(False).to_numpy()
        ax.fill_between(values.index, values.to_numpy(), limit.to_numpy(), where=stressed, interpolate=True,
                        color=_RED, alpha=.3, linewidth=0, zorder=2)
        ax.annotate(f"stress line · now {drawn.iloc[-1]:.0f} bp", (drawn.index[0], drawn.iloc[0]), xytext=(4, 5),
                    textcoords="offset points", va="bottom", ha="left", fontsize=_NOTE, color=_SECOND,
                    path_effects=_halo(), zorder=7)
        top = max(top, drawn.max())
    lowest = values.min()
    pad = (top - lowest) * .09 or 1
    ax.set_ylim(lowest - pad, top + pad * 1.6)
    _yticks(ax, 5)
    _end(ax, values.index[-1], values.iloc[-1], f"{values.iloc[-1]:.0f} bp")
    _dates(ax)


def _margin(figure, signal, range_key):
    history = signal.history
    balance = pd.Series(history.debit_balances.to_numpy(dtype=float) / 1e6, index=pd.DatetimeIndex(history.month))
    frame = pd.DataFrame({"balance": balance, "change": balance.pct_change() * 100})
    visible = _cut(frame, range_key, monthly=True)
    top, bottom = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [1.5, 1]})
    _frame(top, "Margin debt", _range_words(range_key, visible.index, "monthly"))
    top.plot(visible.index, visible.balance, color=_BLUE, linewidth=2.2, solid_capstyle="round", zorder=4,
             marker="o" if len(visible) <= 13 else None, markersize=4)
    spread = visible.balance.max() - visible.balance.min()
    _yticks(top, 4, "${:.1f}T" if spread >= .45 else "${:.2f}T")
    _end(top, visible.index[-1], visible.balance.iloc[-1], f"${visible.balance.iloc[-1]:.2f}T")
    _frame(bottom)
    _heading(bottom, "Monthly change · green = falling")
    change = visible.change.fillna(0)
    # One collection of strokes rather than a patch per month: decades of bars stay cheap to draw.
    thickness = max(1.1, min(15., 190 / len(visible)))
    bottom.vlines(visible.index, 0, change, colors=[_MET if value < 0 else _NEUTRAL for value in change],
                  linewidth=thickness, capstyle="butt", zorder=3)
    bottom.axhline(0, color=_AXIS, linewidth=1, zorder=4)
    _yticks(bottom, 4, "{:+.0f}%")
    _zero(bottom, "0%")
    _dates(bottom)


def _curve(figure, signal, range_key):
    history = signal.history
    columns = {"spread": "last"}
    if "policy_spread" in history and history.policy_spread.notna().any():
        columns["policy_spread"] = "last"
    frame = history.set_index(pd.DatetimeIndex(history["date"]))[list(columns)]
    visible, frequency = _thin(_cut(frame, range_key), columns)
    ax = figure.subplots()
    _frame(ax, "Yield curve", _range_words(range_key, visible.index, frequency) + " · red = inverted")
    spread = visible.spread
    ax.fill_between(spread.index, spread.to_numpy(), 0, where=spread.to_numpy() < 0, interpolate=True,
                    color=_RED, alpha=.22, linewidth=0, zorder=2)
    ax.plot(spread.index, spread, color=_BLUE, linewidth=2.2, zorder=4, label="10-year minus 3-month")
    ends = [(spread.dropna().iloc[-1], _BLUE)]
    if "policy_spread" in visible:
        policy = visible.policy_spread.dropna()
        ax.plot(policy.index, policy, color=_ORANGE, linewidth=1.7, zorder=3, label="2-year minus Fed rate")
        ends.append((policy.iloc[-1], _ORANGE))
    ax.axhline(0, color=_SECOND, linewidth=1.2, zorder=3)
    low, high = ax.get_ylim()
    ax.set_ylim(low, high + (high - low) * .2)
    # Two end labels that would overlap are pushed apart by one line of text.
    gap = (ends[0][0] - ends[1][0]) / (high - low) if len(ends) > 1 else 1
    shifts = [0, 0] if abs(gap) > .07 else ([9, -9] if gap >= 0 else [-9, 9])
    for (value, color), shift in zip(ends, shifts):
        _end(ax, visible.index[-1], value, f"{value:+.2f}", color=color, dy=shift)
    _yticks(ax, 5, "{:+.1f}")
    _zero(ax, "0")
    ax.legend(frameon=False, fontsize=_NOTE, labelcolor=_SECOND, loc="upper left", ncol=1, borderaxespad=.2,
              handlelength=1.4)
    _dates(ax)


def _fed(figure, signal):
    history = signal.history.copy()
    history["meeting_date"] = pd.to_datetime(history["meeting_date"])
    history = history.sort_values("meeting_date").reset_index(drop=True)
    ax = figure.subplots()
    first = history.iloc[0]
    lower, upper = first.get("reference_lower_bps"), first.get("reference_upper_bps")
    title = "Fed rate vs today"
    subtitle = f"Now {lower / 100:.2f}–{upper / 100:.2f}% · odds by each meeting" if pd.notna(lower) and pd.notna(upper) else "Odds by each meeting"
    ax.set_facecolor(_SURFACE)
    _title(figure, title, subtitle)
    parts = (("prob_ease", "delta_ease_pp", _BLUE, "Lower", "white"),
             ("prob_no_change", "delta_no_change_pp", _NEUTRAL, "Same", _INK),
             ("prob_hike", "delta_hike_pp", _ORANGE, "Higher", "white"))
    rows = len(history)
    compared = None
    for position, row in enumerate(history.itertuples()):
        y = rows - 1 - position
        ax.text(0, y + .37, f"By {row.meeting_date:%-d %b %Y}", fontsize=_LABEL, fontweight="bold", color=_INK,
                va="bottom", ha="left")
        left = 0.
        for column, delta_column, color, _, ink in parts:
            value = float(getattr(row, column)) * 100
            if value <= 0:
                continue
            ax.barh(y, value, left=left, height=.56, color=color, edgecolor=_SURFACE, linewidth=2.5, zorder=3)
            middle = left + value / 2
            delta = getattr(row, delta_column, None)
            moved = delta is not None and pd.notna(delta) and abs(delta) >= .5 and value >= 11
            if value >= 11:
                ax.text(middle, y + (.085 if moved else 0), f"{value:.0f}%", ha="center", va="center", fontsize=15,
                        fontweight="bold", color=ink, zorder=4)
            if moved:
                # The change since the previous reading sits inside the share it belongs to.
                ax.text(middle, y - .135, f"{'▲' if delta > 0 else '▼'} {abs(delta):.0f}", ha="center", va="center",
                        fontsize=_NOTE, color=ink, zorder=4)
                compared = getattr(row, "comparison_since", None) or compared
            left += value
    ax.set_xlim(0, 100)
    ax.set_ylim(-.62, rows - .25)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.spines[:].set_visible(False)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=color, label=label) for _, _, color, label, _ in parts], frameon=False,
              fontsize=_LABEL, labelcolor=_SECOND, loc="upper center", bbox_to_anchor=(.5, -.005), ncol=3,
              handlelength=1, handleheight=1, columnspacing=1.6, borderaxespad=0)
    if compared is not None and pd.notna(compared):
        stamp = pd.Timestamp(compared)
        figure.text(.5, 10 / (figure.get_figheight() * 72), f"▲▼ points since {stamp:%-d %b}", fontsize=_NOTE,
                    color=_MUTED, ha="center", va="bottom")
    _place(figure, left=_EDGE, right=1 - _EDGE, top_points=74, bottom_points=78)


def _sector(figure, signal):
    frame = signal.history.iloc[::-1]
    values = frame.earnings_growth_estimate_1y * 100
    coverage = frame.get("estimate_coverage", pd.Series(1., index=frame.index))
    covered = [pd.notna(value) and pd.notna(cover) and cover >= SECTOR_MIN_ESTIMATE_COVERAGE for value, cover in zip(values, coverage)]
    colors = [_NEUTRAL if not ok else _BLUE if value > 0 else _RED for value, ok in zip(values, covered)]
    labels = [textwrap.fill(str(name).replace(" - ", " · "), width=17, break_long_words=False) for name in frame.industry]
    ax = figure.subplots()
    _frame(ax, "Next-year earnings growth", "5 largest US industries · analyst forecasts")
    ax.grid(False)
    bars = ax.barh(labels, values.fillna(0), color=colors, height=.46, zorder=3)
    for bar, value, ok in zip(bars, values, covered):
        text = "too few estimates" if not ok else f"{value:+.0f}%"
        offset = 6 if (pd.isna(value) or value >= 0) else -6
        ax.annotate(text, (0 if pd.isna(value) else value, bar.get_y() + bar.get_height() / 2), xytext=(offset, 0),
                    textcoords="offset points", ha="left" if offset > 0 else "right", va="center",
                    color=_INK if ok else _MUTED, fontsize=_LABEL, fontweight="bold" if ok else "normal")
    ax.axvline(0, color=_AXIS, linewidth=1.2, zorder=4)
    ax.set_xticks([])
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", colors=_INK, labelsize=_LABEL)
    ax.margins(x=.3, y=.08)
    _place(figure, left=.33, right=1 - _EDGE, top_points=74, bottom_points=14)


def _render_signal_chart(signal: SubSignal, range_key: str = DEFAULT_RANGE) -> bytes | None:
    """Return a chart only when this exact signal contains the necessary observations."""
    history = signal.history
    if history is None or history.empty:
        return None
    name = signal.name
    if range_key not in RANGES:
        range_key = DEFAULT_RANGE
    with _LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        tall = name in ("market_dip", "margin_debt")
        figure = Figure(figsize=(6, 6.9 if tall else 5.4), facecolor=_SURFACE)
        FigureCanvasAgg(figure)
        _place(figure)
        if name == "market_dip":
            _market(figure, signal, range_key)
        elif name == "vix":
            _level(figure, signal, range_key, column="vix", title="VIX",
                   thresholds=((VIX_SOFT, f"fear {VIX_SOFT:g}", True), (VIX_STRONG, f"high fear {VIX_STRONG:g}", True)),
                   met_above=VIX_SOFT)
        elif name == "credit":
            _credit(figure, signal, range_key)
        elif name == "margin_debt":
            _margin(figure, signal, range_key)
        elif name == "yield_curve":
            _curve(figure, signal, range_key)
        elif name == "fed_rate":
            _fed(figure, signal)
        elif name == "sector":
            _sector(figure, signal)
        else:
            return None
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=TELEGRAM_CHART_DPI, facecolor=_SURFACE)
        png = buffer.getvalue()
        figure.clear()
        figure.set_canvas(None)
        return png
