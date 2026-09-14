"""Equity Opportunity Analyzer (#8): if buying equities, which broad segment looks attractive.

Fully independent of the buy signal (signals/): that one answers WHEN to buy, this one
answers WHAT. Compares broad segments (US large caps / world small cap / Europe, plus the
value-weighted small-cap variant closest to AVWS) on actual valuations: trailing and true
forward P/E from MSCI index pages (one provider, one methodology, monthly cadence),
cheapness z-scores against our own accumulated snapshot history, plus the expected US rate
path as a small-cap conditional modifier (CME FedWatch expectations, forward-looking,
like everything else here). Informational only: facts plus a transparent rules verdict,
no portfolio instructions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from buysignal.collectors.valuations import valuations_history
from buysignal.config import (
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
    """One (asof, fwd_pe) row per unique MSCI as-of date: daily snapshots repeat the same
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
        return "small caps cheap, but no rate support yet (macro headwind); S&P 500 default"
    # investigate: >30% discounted
    if rate.rate_support:
        return "small caps deeply discounted + rate support: strong candidate, investigate earnings/credit stress"
    return "small caps deeply discounted but no rate support; investigate before acting"


def _rate_view() -> RateView:
    """Expected rate path from the cached CME FedWatch snapshot (forward-looking)."""
    view = RateView()
    try:
        from buysignal.collectors.fed_rate import latest_fedwatch
        from buysignal.signals.rate_signal import _meeting, _probabilities

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
    notes = []

    try:
        history = valuations_history()
    except Exception:
        history = pd.DataFrame(columns=["date", "segment", "fwd_pe", "trailing_pe", "asof"])
        notes.append("no valuations cache yet, run /refresh to fetch")

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
            f"z-scores need {VALUATION_Z_MIN_OBS} monthly MSCI observations, have {obs} "
            "(shown as n/a until enough history accumulates)"
        )

    rate = _rate_view()
    if not rate.horizons:
        notes.append("FedWatch expectations unavailable, run /refresh to fetch")

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


if __name__ == "__main__":
    result = analyze()
    for s in result.segments:
        print(s)
    print(result.rate)
    print(f"band={result.small_cap_band} verdict={result.verdict}")
    for note in result.notes:
        print(f"note: {note}")
