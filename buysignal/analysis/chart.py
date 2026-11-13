"""Draw the valuation comparison as a PNG for Telegram, in the same grammar as the signal charts.

Takes an Opportunity and returns bytes: it computes no verdict and fetches nothing except the
accumulated snapshot history the second panel plots.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd

from buysignal.analysis import signal_charts as style
from buysignal.analysis.opportunity import Opportunity
from buysignal.collectors.valuations import valuations_history
from buysignal.config import TELEGRAM_CHART_DPI
from buysignal.resources import HEAVY_WORK_LOCK

_SERIES = [style._BLUE, style._ORANGE, style._MET, "#eda100"]  # validated categorical order
_HISTORY_PANEL_MIN_OBS = 5  # the over-time panel appears once this many dated snapshots exist
_NAMES = {"sp500": "US large", "world_small": "World small", "world_small_value": "Small value", "europe": "Europe"}


def render_chart(opportunity: Opportunity, history: pd.DataFrame | None = None, dips: dict | None = None) -> bytes:
    with HEAVY_WORK_LOCK:
        return _render_chart(opportunity, history, dips or {})


def _label(view) -> str:
    market = _NAMES.get(view.name, view.label)
    return f"{view.etf}\n{market}" if view.etf else market


def _render_chart(opportunity: Opportunity, history: pd.DataFrame | None, dips: dict) -> bytes:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    if history is None:
        try:
            history = valuations_history()
        except Exception:
            history = pd.DataFrame(columns=["date", "segment", "fwd_pe"])
    dated = history[history["segment"] == "sp500"]["fwd_pe"].notna().sum() if not history.empty else 0
    with_history = dated >= _HISTORY_PANEL_MIN_OBS

    figure = Figure(figsize=(6, 7.4 if with_history else 5.0), facecolor=style._SURFACE)
    FigureCanvasAgg(figure)
    if with_history:
        ax, over_time = figure.subplots(2, 1, gridspec_kw={"height_ratios": [1.25, 1]})
    else:
        ax, over_time = figure.subplots(), None

    # Cheapest first; the US bar is the grey reference the others are read against.
    priced = sorted((view for view in opportunity.segments if view.fwd_pe is not None), key=lambda view: -view.fwd_pe)
    style._frame(ax, "Forward P/E", "price ÷ next year's earnings · lower = cheaper")
    ax.grid(False)
    bars = ax.barh([_label(view) for view in priced], [view.fwd_pe for view in priced], height=.36, zorder=3,
                   color=[style._NEUTRAL if view.name == "sp500" else style._BLUE for view in priced])
    for bar, view in zip(bars, priced):
        y = bar.get_y() + bar.get_height() / 2
        ax.annotate(f"{view.fwd_pe:.1f}", (view.fwd_pe, y), xytext=(6, 0), textcoords="offset points", va="center",
                    ha="left", fontsize=style._LABEL, fontweight="bold", color=style._INK)
        note = []
        if view.name != "sp500" and view.discount_vs_spx is not None:
            note.append(f"{abs(view.discount_vs_spx):.0%} {'below' if view.discount_vs_spx >= 0 else 'above'} US")
        dip = dips.get(view.etf, {}).get("drawdown") if view.etf else None
        if dip is not None:
            note.append("at 3-month high" if dip == 0 else f"{abs(dip):.1%} below high")
        if note:
            # Under the bar rather than inside it: a short bar cannot hold its own note.
            ax.annotate(" · ".join(note), (0, bar.get_y()), xytext=(0, -4), textcoords="offset points", va="top",
                        ha="left", fontsize=style._NOTE, color=style._SECOND, zorder=5)
    ax.set_xticks([])
    ax.spines["bottom"].set_visible(False)
    ax.tick_params(axis="y", colors=style._INK, labelsize=style._LABEL)
    ax.margins(x=.14, y=.12)

    if with_history:
        style._frame(over_time)
        style._heading(over_time, "Forward P/E over time")
        for color, view in zip(_SERIES, opportunity.segments):
            rows = history[history["segment"] == view.name].dropna(subset=["fwd_pe"])
            if rows.empty:
                continue
            over_time.plot(rows["date"], rows["fwd_pe"], color=color, linewidth=2, solid_joinstyle="round", zorder=3,
                           label=view.etf or _NAMES.get(view.name, view.label))
        over_time.legend(frameon=False, fontsize=style._NOTE, labelcolor=style._SECOND, loc="upper left", ncol=4,
                         bbox_to_anchor=(0, -.16), borderaxespad=0, handlelength=1.2, columnspacing=1.2)
        style._yticks(over_time, 4)
        style._dates(over_time)
    style._place(figure, left=.24, right=1 - style._EDGE, top_points=74, bottom_points=58 if with_history else 14, gap=.34)

    buffer = BytesIO()
    figure.savefig(buffer, format="png", dpi=TELEGRAM_CHART_DPI, facecolor=style._SURFACE)
    figure.clear()
    figure.set_canvas(None)
    return buffer.getvalue()
