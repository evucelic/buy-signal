"""Snapshot-based PNG charts for detailed Telegram reports; no network or file reads."""

from __future__ import annotations

from io import BytesIO
from collections import OrderedDict
import hashlib
import threading

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
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", color=_INK, pad=13)
    ax.set_ylabel(ylabel, fontsize=10, color=_MUTED)
    ax.tick_params(colors=_MUTED, labelsize=9)
    ax.grid(axis="y", color=_GRID, linewidth=.8, zorder=0)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(_GRID)
    ax.margins(x=.02)


def _legend(ax):
    ax.legend(frameon=False, fontsize=9, labelcolor=_MUTED, loc="best")


def render_signal_chart(signal: SubSignal) -> bytes | None:
    global _CACHE_BYTES
    if signal.history is None or signal.history.empty:
        return None
    hashed = pd.util.hash_pandas_object(signal.history, index=True).values.tobytes()
    key = (signal.name, signal.observed_at, tuple(signal.history.columns), hashlib.sha256(hashed).digest(), TELEGRAM_CHART_DPI)
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
        figure = Figure(figsize=(9, 6.8), facecolor=_BG, layout="constrained")
        FigureCanvasAgg(figure)
        name = signal.name
        if name == "market_dip":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2.1, 1]})
            price = history["SPY"].dropna()
            average = price.rolling(200).mean()
            visible = price.tail(252)
            axes[0].plot(visible.index, visible, color=_BLUE, linewidth=2, label="SPY adjusted close")
            axes[0].plot(visible.index, average.reindex(visible.index), color=_ORANGE, linewidth=1.8, label="200-session average")
            axes[0].scatter(visible.index[-1], visible.iloc[-1], color=_BLUE, s=35, zorder=4)
            _style(axes[0], "SPY | price and long-term trend", "Adjusted price ($)")
            _legend(axes[0])
            peak = price.shift(1).rolling(MARKET_DRAWDOWN_SESSIONS).max()
            dd = ((price / peak - 1).clip(upper=0) * 100).reindex(visible.index)
            axes[1].fill_between(dd.index, dd.to_numpy(), 0, color=_BLUE, alpha=.16)
            axes[1].plot(dd.index, dd, color=_BLUE, linewidth=1.5)
            axes[1].axhline(MARKET_DIP_THRESHOLD * 100, color=_RED, linestyle="--", linewidth=1.2, label=f"Entry threshold {MARKET_DIP_THRESHOLD:.0%}")
            _style(axes[1], "Distance from the prior 63-session closing high", "Drawdown (%)")
            _legend(axes[1])
        elif name == "fed_rate":
            has_delta = "delta_hike_pp" in history and history.delta_hike_pp.notna().any()
            axes = figure.subplots(2 if has_delta else 1, 1, gridspec_kw={"height_ratios": [2, 1]} if has_delta else None)
            ax = axes[0] if has_delta else axes
            labels = [f"{row.horizon.replace('_', ' ')}\n{pd.Timestamp(row.meeting_date):%d %b %Y}" for row in history.itertuples()]
            bottom = np.zeros(len(history))
            for column, color, label in (("prob_ease", _TEAL, "Ease"), ("prob_no_change", _BLUE, "No change"), ("prob_hike", _ORANGE, "Hike")):
                values = history[column].to_numpy(dtype=float) * 100
                ax.bar(labels, values, bottom=bottom, color=color, width=.52, label=label, zorder=3)
                for i, value in enumerate(values):
                    if value >= 7:
                        ax.text(i, bottom[i] + value / 2, f"{value:.1f}%", ha="center", va="center", fontsize=10, color="white", fontweight="bold")
                bottom += values
            ax.set_ylim(0, 115)
            _style(ax, "FedWatch | probabilities relative to the current target range", "Probability (%)")
            ax.legend(frameon=False, fontsize=9, labelcolor=_MUTED, loc="upper left", ncol=3)
            if has_delta:
                valid = history.delta_hike_pp.notna()
                deltas = history.loc[valid, "delta_hike_pp"].to_numpy(dtype=float)
                axes[1].bar(np.array(labels)[valid], deltas, width=.4, color=[_ORANGE if x > 0 else _TEAL for x in deltas], zorder=3)
                axes[1].axhline(0, color=_GRID)
                _style(axes[1], "Hike-probability change since the previous comparable refresh", "Percentage points")
        elif name == "vix":
            ax = figure.subplots()
            frame = history.tail(126)
            ax.plot(frame.date, frame.vix, color=_BLUE, linewidth=2)
            ax.axhline(VIX_SOFT, color=_ORANGE, linestyle="--", label=f"Fear condition {VIX_SOFT:g}")
            ax.axhline(VIX_STRONG, color=_RED, linestyle="--", label=f"High fear {VIX_STRONG:g}")
            _style(ax, "VIX | daily last observation", "VIX")
            _legend(ax)
        elif name == "credit":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2, 1]})
            frame = history.tail(252)
            axes[0].plot(frame.date, frame.spread, color=_BLUE, linewidth=2)
            _style(axes[0], "Corporate credit | high-yield option-adjusted spread", "Spread (percentage points)")
            delta = history.set_index("date").spread.diff(21).reindex(frame.date) * 100
            axes[1].plot(frame.date, delta.to_numpy(), color=_ORANGE, linewidth=1.6)
            axes[1].axhline(0, color=_GRID)
            _style(axes[1], "Change over 21 observations | positive means widening", "Basis points")
        elif name == "margin_debt":
            axes = figure.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [2, 1]})
            frame = history.tail(30)
            axes[0].plot(frame.month, frame.debit_balances / 1e6, color=_BLUE, linewidth=2, marker="o", markersize=3)
            _style(axes[0], "FINRA margin debt | monthly observations", "USD trillions")
            changes = frame.debit_balances.pct_change() * 100
            axes[1].bar(frame.month, changes, width=18, color=[_TEAL if x < 0 else _ORANGE for x in changes])
            axes[1].axhline(0, color=_GRID)
            _style(axes[1], "Monthly balance change | negative means deleveraging", "Change (%)")
        elif name == "sector":
            ax = figure.subplots()
            frame = history.iloc[::-1]
            values = frame.earnings_growth_estimate_1y * 100
            coverage = frame.get("estimate_coverage", pd.Series(0., index=frame.index))
            colors = [_MUTED if cov < SECTOR_MIN_ESTIMATE_COVERAGE else _TEAL if x > 0 else _RED for x, cov in zip(values, coverage)]
            bars = ax.barh(frame.industry, values, color=colors, height=.5, zorder=3)
            for bar, value in zip(bars, values):
                if pd.notna(value):
                    ax.annotate(f"{value:+.1f}%", (value, bar.get_y()+bar.get_height()/2), xytext=(5 if value>=0 else -5, 0), textcoords="offset points", ha="left" if value>=0 else "right", va="center", color=_INK, fontsize=9)
            ax.axvline(0, color=_GRID)
            _style(ax, "Largest industries | sampled next-year EPS growth", "")
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
        figure.supxlabel(f"Source observation: {signal.observed_at or 'see report'}", color=_MUTED, fontsize=9)
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=TELEGRAM_CHART_DPI, facecolor=_BG)
        png = buffer.getvalue()
        figure.clear()
        figure.set_canvas(None)
        return png
