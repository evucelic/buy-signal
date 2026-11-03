"""Snapshot-based PNG charts for detailed Telegram reports; no network or file reads."""

from __future__ import annotations

from io import BytesIO
from collections import OrderedDict
import hashlib
import threading
import textwrap

import numpy as np
import pandas as pd

from buysignal.config import MARKET_DIP_THRESHOLD, MARKET_DRAWDOWN_SESSIONS, VIX_SOFT, VIX_STRONG, TELEGRAM_CHART_DPI, CHART_CACHE_MAX_BYTES, SECTOR_MIN_ESTIMATE_COVERAGE
from buysignal.resources import HEAVY_WORK_LOCK
from buysignal.signals.base import SubSignal

_LOCK = HEAVY_WORK_LOCK
_CACHE_LOCK = threading.Lock()
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_BG, _INK, _MUTED, _GRID = "#fbfcfe", "#142338", "#607085", "#e4eaf1"
_BLUE, _TEAL, _ORANGE, _RED = "#2864dc", "#128675", "#d78b16", "#cd4756"


def _style(ax, title, ylabel=""):
    ax.set_facecolor(_BG)
    ax.set_title(textwrap.fill(title, width=42, break_long_words=False), loc="left", fontsize=14, fontweight="bold", color=_INK, pad=13)
    ax.set_ylabel(ylabel, fontsize=11, color=_MUTED)
    ax.tick_params(colors=_MUTED, labelsize=11)
    ax.grid(axis="y", color=_GRID, linewidth=.8, zorder=0)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(_GRID)
    ax.margins(x=.02)


def _legend(ax):
    ax.legend(frameon=False, fontsize=11, labelcolor=_MUTED, loc="best")


def render_signal_chart(signal: SubSignal) -> bytes | None:
    global _CACHE_BYTES
    if signal.history is None or signal.history.empty:
        return None
    hashed = pd.util.hash_pandas_object(signal.history, index=True).values.tobytes()
    key = (signal.name, signal.observed_at, signal.metrics.get("chart_index"), tuple(signal.history.columns), hashlib.sha256(hashed).digest(), TELEGRAM_CHART_DPI)
    with _LOCK:
        with _CACHE_LOCK:
            if key in _CACHE:
                _CACHE.move_to_end(key)
                return _CACHE[key]
        png = _render_signal_chart(signal)
        if png is not None and len(png) <= CHART_CACHE_MAX_BYTES:
            with _CACHE_LOCK:
                while _CACHE and _CACHE_BYTES + len(png) > CHART_CACHE_MAX_BYTES:
                    _, evicted = _CACHE.popitem(last=False)
                    _CACHE_BYTES -= len(evicted)
                _CACHE[key] = png
                _CACHE_BYTES += len(png)
        return png


def _render_signal_chart(signal: SubSignal) -> bytes | None:
    """Return a chart only when this exact signal contains the necessary observations."""
    history = signal.history
    if history is None or history.empty:
        return None
    with _LOCK:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        figure = Figure(figsize=(6, 7.6), facecolor=_BG, layout="constrained")
        FigureCanvasAgg(figure)
        name = signal.name
        if name == "market_dip":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2.1, 1]})
            index_name = signal.metrics.get("chart_index", "SPY")
            price = history[index_name].dropna()
            average = price.rolling(200).mean()
            visible = price.tail(252)
            axes[0].plot(visible.index, visible, color=_BLUE, linewidth=2, label=f"{index_name} adjusted close")
            axes[0].plot(visible.index, average.reindex(visible.index), color=_ORANGE, linewidth=1.8, label="200-session average")
            axes[0].scatter(visible.index[-1], visible.iloc[-1], color=_BLUE, s=35, zorder=4)
            _style(axes[0], f"{index_name} | daily prices", "Price (USD)" if index_name == "SPY" else "Index points")
            _legend(axes[0])
            peak = price.shift(1).rolling(MARKET_DRAWDOWN_SESSIONS).max()
            dd = ((price / peak - 1).clip(upper=0) * 100).reindex(visible.index)
            axes[1].fill_between(dd.index, dd.to_numpy(), 0, color=_BLUE, alpha=.16)
            axes[1].plot(dd.index, dd, color=_BLUE, linewidth=1.5)
            if index_name == "SPY":
                axes[1].axhline(MARKET_DIP_THRESHOLD * 100, color=_RED, linestyle="--", linewidth=1.2, label=f"SPY entry threshold {MARKET_DIP_THRESHOLD:.0%}")
            _style(axes[1], "Drawdown | prior 63 sessions", "Drawdown (%)")
            if index_name == "SPY":
                _legend(axes[1])
        elif name == "fed_rate":
            ax = figure.subplots()
            labels = [f"{row.horizon.replace('_', ' ')}\n{pd.Timestamp(row.meeting_date):%d %b %Y}" for row in history.itertuples()]
            bottom = np.zeros(len(history))
            for column, color, label in (("prob_ease", _TEAL, "Ease"), ("prob_no_change", _BLUE, "No change"), ("prob_hike", _ORANGE, "Hike")):
                values = history[column].to_numpy(dtype=float) * 100
                ax.bar(labels, values, bottom=bottom, color=color, width=.52, label=label, zorder=3)
                for i, value in enumerate(values):
                    if value >= 7:
                        ax.text(i, bottom[i] + value / 2, f"{value:.1f}%", ha="center", va="center", fontsize=12, color="white", fontweight="bold")
                bottom += values
            ax.set_ylim(0, 115)
            _style(ax, "FedWatch | target rate vs current range", "Probability (%)")
            ax.legend(frameon=False, fontsize=11, labelcolor=_MUTED, loc="upper left", ncol=3)
        elif name == "vix":
            ax = figure.subplots()
            frame = history.tail(126)
            ax.plot(frame.date, frame.vix, color=_BLUE, linewidth=2)
            ax.axhline(VIX_SOFT, color=_ORANGE, linestyle="--", label=f"Fear condition {VIX_SOFT:g}")
            ax.axhline(VIX_STRONG, color=_RED, linestyle="--", label=f"High fear {VIX_STRONG:g}")
            highs = "high" in frame and len(frame) >= 20 and frame.high.tail(20).notna().all()
            peak = (frame.high if highs else frame.vix).tail(20).max()
            label = "20-session high" if highs else "20-session closing high"
            ax.axhline(peak, color=_TEAL, linestyle=":", linewidth=1.2, label=f"{label} {peak:.1f}")
            ax.scatter(frame.date.iloc[-1], frame.vix.iloc[-1], color=_BLUE, s=30, zorder=4)
            _style(ax, "VIX | daily observations", "VIX points")
            _legend(ax)
        elif name == "credit":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2, 1]})
            frame = history.tail(252)
            axes[0].plot(frame.date, frame.spread * 100, color=_BLUE, linewidth=2)
            _style(axes[0], "US high-yield credit | daily OAS", "Spread (bp)")
            delta = history.set_index("date").spread.diff(21).reindex(frame.date) * 100
            axes[1].plot(frame.date, delta.to_numpy(), color=_ORANGE, linewidth=1.6)
            axes[1].axhline(0, color=_GRID)
            _style(axes[1], "21-session change | + widening / − narrowing", "Change (bp)")
        elif name == "margin_debt":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2, 1]})
            frame = history.tail(12)
            axes[0].plot(frame.month, frame.debit_balances, color=_BLUE, linewidth=2, marker="o", markersize=3)
            _style(axes[0], "FINRA margin debt | monthly", "USD million")
            axes[0].ticklabel_format(axis="y", style="plain", useOffset=False)
            from matplotlib.ticker import StrMethodFormatter
            axes[0].yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            changes = history.debit_balances.pct_change().reindex(frame.index) * 100
            axes[1].bar(frame.month, changes, width=18, color=[_TEAL if x < 0 else _ORANGE for x in changes])
            axes[1].axhline(0, color=_GRID)
            _style(axes[1], "Month-on-month balance change", "Change (%)")
        elif name == "sector":
            ax = figure.subplots()
            frame = history.iloc[::-1]
            values = frame.earnings_growth_estimate_1y * 100
            coverage = frame.get("estimate_coverage", pd.Series(0., index=frame.index))
            colors = [_MUTED if cov < SECTOR_MIN_ESTIMATE_COVERAGE else _TEAL if x > 0 else _RED for x, cov in zip(values, coverage)]
            labels = [textwrap.fill(str(name), width=20, break_long_words=False) for name in frame.industry]
            bars = ax.barh(labels, values, color=colors, height=.5, zorder=3)
            for bar, value in zip(bars, values):
                if pd.notna(value):
                    ax.annotate(f"{value:+.1f}%", (value, bar.get_y()+bar.get_height()/2), xytext=(5 if value>=0 else -5, 0), textcoords="offset points", ha="left" if value>=0 else "right", va="center", color=_INK, fontsize=11)
            ax.axvline(0, color=_GRID)
            _style(ax, "Industry EPS | FY+1", "")
            ax.set_xlabel("Forecast growth (%)", color=_MUTED)
            ax.margins(x=.25)
        elif name == "yield_curve":
            ax = figure.subplots()
            ax.plot(history.date, history.spread, color=_BLUE, linewidth=1.8, label="10y - 3m Treasury")
            if "policy_spread" in history:
                ax.plot(history.date, history.policy_spread, color=_TEAL, linewidth=1.6, label="2y Treasury - effective fed funds")
            ax.axhline(0, color=_ORANGE, linestyle="--", linewidth=1)
            _style(ax, "Yield-curve context | daily observations", "Percentage points")
            _legend(ax)
        else:
            return None
        # Monthly releases get a tick for every month. Daily charts share the same
        # unobtrusive month/year style without repeating date explanations in captions.
        from matplotlib.dates import MonthLocator, DateFormatter, AutoDateLocator, ConciseDateFormatter
        if name not in ("sector", "fed_rate"):
            bottom_axis = figure.axes[-1]
            if name == "margin_debt":
                bottom_axis.xaxis.set_major_locator(MonthLocator(interval=1))
                bottom_axis.xaxis.set_major_formatter(DateFormatter("%b\n%Y"))
                bottom_axis.tick_params(axis="x", labelsize=10)
            else:
                locator = AutoDateLocator(minticks=4, maxticks=8)
                bottom_axis.xaxis.set_major_locator(locator)
                bottom_axis.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        if signal.observed_at:
            figure.supxlabel(f"As of {signal.observed_at}", color=_MUTED, fontsize=9)
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=TELEGRAM_CHART_DPI, facecolor=_BG)
        png = buffer.getvalue()
        figure.clear()
        figure.set_canvas(None)
        return png
