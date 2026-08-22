"""Equity Opportunity Analyzer (#8): if buying equities, which broad segment looks attractive.

Fully independent of the buy signal (signals/) — that one answers WHEN to buy, this one
answers WHAT. Compares broad segments (US large caps / world small cap / Europe, plus the
value-weighted small-cap variant closest to AVWS) on actual valuations: trailing and true
forward P/E from MSCI index pages (one provider, one methodology, monthly cadence),
cheapness z-scores against our own accumulated snapshot history, plus the expected US rate
path as a small-cap conditional modifier (CME FedWatch expectations — forward-looking,
like everything else here). Informational only: facts plus a transparent rules verdict,
no portfolio instructions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from io import BytesIO

import pandas as pd

from collectors.valuations import valuations_history
from config import (
    OPPORTUNITY_SEGMENTS,
    SMALL_CAP_DISCOUNT_BANDS,
    SMALL_CAP_EASING_OBS,
    VALUATION_Z_MIN_OBS,
)


@dataclass
class SegmentView:
    name: str
    label: str
    fwd_pe: float | None = None
    asof: str | None = None             # MSCI's own as-of date for both ratios
    trailing_pe: float | None = None
    fwd_z: float | None = None          # cheapness z: -(pe - mean)/std over own monthly history
    ratio_vs_spx: float | None = None   # fwd_pe / US large cap fwd_pe
    discount_vs_spx: float | None = None  # 1 - ratio_vs_spx
    rel_z: float | None = None          # cheapness z of log(fwd ratio vs US large caps)


@dataclass
class HorizonView:
    label: str          # "nearest" / "6m" / "1y"
    meeting_date: str   # iso date of the FedWatch meeting row
    ease: float
    no_change: float
    hike: float
    state: str          # dominant expectation: "ease" / "no_change" / "hike" / "mixed"


@dataclass
class RateView:
    horizons: list[HorizonView] = field(default_factory=list)
    consecutive_easing: int = 0  # horizons expected to ease, counted from nearest
    rate_support: bool = False


@dataclass
class Opportunity:
    segments: list[SegmentView]
    rate: RateView
    small_cap_band: str | None   # "little" / "mild" / "candidate" / "investigate"
    verdict: str
    notes: list[str]
    history_obs: int             # unique monthly MSCI observations backing the z-scores
    buy_signal_state: str | None = None  # context only, filled by the caller


def _cheapness_z(series: pd.Series, current: float) -> float | None:
    """-(current - mean)/std over the history; positive = cheaper than own norm."""
    series = series.dropna()
    if len(series) < VALUATION_Z_MIN_OBS:
        return None
    std = series.std()
    if not std or math.isnan(std):
        return None
    return -(current - series.mean()) / std


def _monthly_series(history: pd.DataFrame, segment: str) -> pd.DataFrame:
    """One (asof, fwd_pe) row per unique MSCI as-of date — daily snapshots repeat the same
    monthly value, which would understate the std if fed to the z-score raw.
    """
    rows = history[history["segment"] == segment].dropna(subset=["fwd_pe"])
    return rows.drop_duplicates(subset=["asof"], keep="last")[["asof", "fwd_pe"]]


def _discount_band(discount: float) -> str:
    little, mild, candidate = SMALL_CAP_DISCOUNT_BANDS
    if discount < little:
        return "little"
    if discount < mild:
        return "mild"
    if discount <= candidate:
        return "candidate"
    return "investigate"


def _verdict(band: str | None, rate: RateView) -> str:
    """The small-cap decision matrix (valuation band x rate regime), as one plain line."""
    if band is None:
        return "insufficient data for a small-cap read"
    if band in ("little", "mild"):
        if rate.rate_support:
            return "S&P 500 default; small caps on watch (rate support present, discount insufficient)"
        return "S&P 500 default (small caps not sufficiently discounted, no rate support)"
    if band == "candidate":
        if rate.rate_support:
            return "small caps: valuation + rate conditions both met (candidate)"
        return "small caps cheap, but no rate support yet — macro headwind; S&P 500 default"
    # investigate: >30% discounted
    if rate.rate_support:
        return "small caps deeply discounted + rate support — strong candidate, investigate earnings/credit stress"
    return "small caps deeply discounted but no rate support — investigate before acting"


def _rate_view() -> RateView:
    """Expected rate path from the cached CME FedWatch snapshot (forward-looking)."""
    view = RateView()
    try:
        from collectors.fed_rate import latest_fedwatch
        from signals.rate_signal import _meeting, _probabilities

        df = latest_fedwatch().sort_values("meeting_date").reset_index(drop=True)
        for label, horizon in (("nearest", "nearest"), ("6m", "six_month"), ("1y", "one_year")):
            meeting = _meeting(df, horizon)
            ease, no_change, hike = _probabilities(meeting)
            if ease > max(no_change, hike):
                state = "ease"
            elif hike > max(ease, no_change):
                state = "hike"
            elif no_change > max(ease, hike):
                state = "no_change"
            else:
                state = "mixed"
            view.horizons.append(
                HorizonView(label, meeting["meeting_date"].date().isoformat(), ease, no_change, hike, state)
            )
    except Exception:  # empty/malformed fedwatch cache must degrade, not kill the caller
        view.horizons.clear()
        return view

    for horizon_view in view.horizons:
        if horizon_view.state != "ease":
            break
        view.consecutive_easing += 1
    view.rate_support = view.consecutive_easing >= SMALL_CAP_EASING_OBS
    return view


def analyze() -> Opportunity:
    notes = [spec["proxy_note"] for spec in OPPORTUNITY_SEGMENTS.values() if spec["proxy_note"]]

    try:
        history = valuations_history()
    except Exception:
        history = pd.DataFrame(columns=["date", "segment", "fwd_pe", "trailing_pe", "asof"])
        notes.append("no valuations cache yet — /refresh to fetch")

    segments = []
    for name, spec in OPPORTUNITY_SEGMENTS.items():
        view = SegmentView(name, spec["label"])
        rows = history[history["segment"] == name]
        if not rows.empty:
            latest = rows.iloc[-1]
            view.fwd_pe = None if pd.isna(latest["fwd_pe"]) else float(latest["fwd_pe"])
            view.asof = None if pd.isna(latest["asof"]) else str(latest["asof"])
            view.trailing_pe = None if pd.isna(latest["trailing_pe"]) else float(latest["trailing_pe"])
            if view.fwd_pe is not None:
                view.fwd_z = _cheapness_z(_monthly_series(history, name)["fwd_pe"], view.fwd_pe)
        segments.append(view)

    by_name = {s.name: s for s in segments}
    spx = by_name["sp500"]
    if spx.fwd_pe:
        spx.ratio_vs_spx, spx.discount_vs_spx = 1.0, 0.0
        spx_monthly = _monthly_series(history, "sp500").rename(columns={"fwd_pe": "spx"})
        for view in segments:
            if view is spx or view.fwd_pe is None:
                continue
            view.ratio_vs_spx = view.fwd_pe / spx.fwd_pe
            view.discount_vs_spx = 1.0 - view.ratio_vs_spx
            joined = _monthly_series(history, view.name).merge(spx_monthly, on="asof").dropna()
            if not joined.empty:
                log_ratio = (joined["fwd_pe"] / joined["spx"]).apply(math.log)
                view.rel_z = _cheapness_z(log_ratio, log_ratio.iloc[-1])

    obs = len(_monthly_series(history, "sp500"))
    if obs < VALUATION_Z_MIN_OBS:
        notes.append(
            f"z-scores need {VALUATION_Z_MIN_OBS} monthly MSCI observations, have {obs} — "
            "shown as n/a until enough history accumulates"
        )

    rate = _rate_view()
    if not rate.horizons:
        notes.append("FedWatch expectations unavailable — /refresh to fetch")

    small = by_name["world_small"]
    band = _discount_band(small.discount_vs_spx) if small.discount_vs_spx is not None else None

    return Opportunity(
        segments=segments,
        rate=rate,
        small_cap_band=band,
        verdict=_verdict(band, rate),
        notes=notes,
        history_obs=obs,
    )


# --- chart -------------------------------------------------------------------
# Light-mode palette from the validated reference set (dataviz skill): categorical
# slots 1-4, text tokens for all ink — marks carry color, text never does.
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
    ax.set_title("Segment valuations — P/E", color=_INK, fontsize=12, loc="left", pad=12)
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
        ax2.set_title("Forward P/E — accumulated daily snapshots", color=_INK, fontsize=11, loc="left", pad=10)
        ax2.margins(x=0.12)

    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=150, facecolor=_SURFACE)
    plt.close(fig)
    return buffer.getvalue()


if __name__ == "__main__":
    result = analyze()
    for s in result.segments:
        print(s)
    print(result.rate)
    print(f"band={result.small_cap_band} verdict={result.verdict}")
    for note in result.notes:
        print(f"note: {note}")
