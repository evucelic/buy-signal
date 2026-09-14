"""Draw the segment-valuation comparison as a PNG for Telegram.

Takes an Opportunity and returns bytes: it computes no verdict and fetches nothing except the
accumulated snapshot history the second panel plots, so the numbers can change without this
file moving, and the palette can change without opportunity.py moving.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd

from buysignal.analysis.opportunity import Opportunity
from buysignal.collectors.valuations import valuations_history

# Light-mode palette from the validated reference set (dataviz skill): categorical
# slots 1-4, text tokens for all ink; marks carry color, text never does.
_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_INK_MUTED = "#898781"
_GRID = "#e1e0d9"
_BASELINE = "#c3c2b7"
_SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # blue, orange, aqua, yellow

_HISTORY_PANEL_MIN_OBS = 5  # fwd-P/E time-series panel appears once this many days exist


def render_chart(opportunity: Opportunity, history: pd.DataFrame | None = None) -> bytes:
    """Render the valuation comparison as a PNG (grouped bars; history panel once data exists)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if history is None:
        try:
            history = valuations_history()
        except Exception:
            history = pd.DataFrame(columns=["date", "segment", "fwd_pe"])

    dates = history[history["segment"] == "sp500"]["fwd_pe"].notna().sum() if not history.empty else 0
    with_history = dates >= _HISTORY_PANEL_MIN_OBS

    if with_history:
        fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8, 7.5), height_ratios=[3, 2])
    else:
        fig, ax = plt.subplots(figsize=(8, 4.5))
    fig.patch.set_facecolor(_SURFACE)

    # --- panel 1: grouped bars, forward vs trailing P/E per segment ---
    ax.set_facecolor(_SURFACE)
    labels = [s.label for s in opportunity.segments]
    fwd = [s.fwd_pe for s in opportunity.segments]
    trail = [s.trailing_pe for s in opportunity.segments]
    x = range(len(labels))
    width = 0.28  # thin marks with air in the band
    gap = 0.04    # surface gap between the paired bars

    for offset, values, color, label in (
        (-(width + gap) / 2, fwd, _SERIES[0], "Forward P/E"),
        ((width + gap) / 2, trail, _SERIES[1], "Trailing P/E"),
    ):
        positions = [i + offset for i in x]
        heights = [v if v is not None else 0 for v in values]
        ax.bar(positions, heights, width, color=color, label=label, zorder=3)
        for pos, value in zip(positions, values):
            ax.text(
                pos,
                (value if value is not None else 0) + 0.35,
                f"{value:.1f}" if value is not None else "n/a",
                ha="center",
                va="bottom",
                fontsize=9,
                color=_INK,
            )

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, color=_INK_SECONDARY, fontsize=10)
    ax.tick_params(axis="y", colors=_INK_MUTED, labelsize=9)
    ax.grid(axis="y", color=_GRID, linewidth=1, zorder=0)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(_BASELINE)
    ax.set_title("Segment valuations: P/E", color=_INK, fontsize=12, loc="left", pad=12)
    ax.legend(frameon=False, labelcolor=_INK_SECONDARY, fontsize=9, loc="upper right")
    ax.margins(y=0.15)

    # --- panel 2: forward P/E over time per segment (from accumulated snapshots) ---
    if with_history:
        ax2.set_facecolor(_SURFACE)
        for color, view in zip(_SERIES, opportunity.segments):
            rows = history[history["segment"] == view.name].dropna(subset=["fwd_pe"])
            if rows.empty:
                continue
            ax2.plot(rows["date"], rows["fwd_pe"], color=color, linewidth=2, solid_joinstyle="round", zorder=3)
            ax2.plot(
                rows["date"].iloc[-1],
                rows["fwd_pe"].iloc[-1],
                "o",
                color=color,
                markersize=8,
                markeredgecolor=_SURFACE,
                markeredgewidth=2,
                zorder=4,
            )
            ax2.annotate(
                view.label,
                (rows["date"].iloc[-1], rows["fwd_pe"].iloc[-1]),
                textcoords="offset points",
                xytext=(8, 0),
                color=_INK_SECONDARY,
                fontsize=9,
                va="center",
            )
        ax2.tick_params(colors=_INK_MUTED, labelsize=9)
        ax2.grid(axis="y", color=_GRID, linewidth=1, zorder=0)
        for spine in ("top", "right", "left"):
            ax2.spines[spine].set_visible(False)
        ax2.spines["bottom"].set_color(_BASELINE)
        ax2.set_title("Forward P/E, accumulated daily snapshots", color=_INK, fontsize=11, loc="left", pad=10)
        ax2.margins(x=0.12)

    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=150, facecolor=_SURFACE)
    plt.close(fig)
    return buffer.getvalue()
