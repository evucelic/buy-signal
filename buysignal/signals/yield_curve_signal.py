"""Yield-curve context (#7), never a checklist condition.

Both spreads are read in complete 3-week buckets. The 10y-3m curve sets the regime. The
2y-minus-fed-funds spread only reports: below zero means cuts are priced, which preceded soft
landings (1995, 1998, 2019) as often as recessions (1989, 2001, 2007).
"""

from __future__ import annotations

import pandas as pd

from buysignal.collectors.yield_curve import yield_curve_history
from buysignal.config import (
    POLICY_SPREAD_NEG_BUCKETS,
    YIELD_CURVE_BUCKET_DAYS,
    YIELD_CURVE_DEEP_INVERSION,
    YIELD_CURVE_STEEP,
)
from buysignal.signals.base import SubSignal

_BUCKET = f"{YIELD_CURVE_BUCKET_DAYS}D"

_BLURBS = {
    "deep_inversion": "deep persistent inversion, high recession risk",
    "inverted": "restrictive late-cycle environment",
    "flat": "neutral or transitional environment",
    "steep": "supportive growth/liquidity regime",
}


def _classify(complete: pd.Series) -> str:
    """Regime from complete-bucket averages (last = most recent). Needs at least one bucket."""
    last = complete.iloc[-1]
    if len(complete) >= 2 and (complete.iloc[-2:] <= YIELD_CURVE_DEEP_INVERSION).all():
        return "deep_inversion"
    if last < 0:
        return "inverted"
    if last < YIELD_CURVE_STEEP:
        return "flat"
    return "steep"


def _bucketed(history: pd.DataFrame) -> pd.DataFrame:
    """Per-bucket means of both spreads and each bucket's last date.

    Buckets are fixed from the start of the series; the last one is still filling.
    """
    work = history.copy()
    work["thru"] = work["date"]

    aggregation = {"spread": "mean", "thru": "max"}
    if "policy_spread" in work.columns:
        aggregation["policy_spread"] = "mean"

    return work.set_index("date").resample(_BUCKET).agg(aggregation).dropna(subset=["spread"])


def _buckets_below(complete: pd.DataFrame) -> int:
    """Consecutive complete buckets, ending with the latest, averaged below fed funds.

    A gap breaks the run: the collector carries the last policy value over a missing leg.
    """
    if "policy_spread" not in complete.columns:
        return 0

    count = 0
    for value in reversed(complete["policy_spread"].tolist()):
        if pd.isna(value) or value >= 0:
            break
        count += 1
    return count


def score() -> SubSignal:
    history = yield_curve_history()
    latest = history.iloc[-1]

    buckets = _bucketed(history)
    complete = buckets.iloc[:-1]  # the newest bucket is still filling

    state = _classify(complete["spread"]) if not complete.empty else "flat"
    has_policy = "policy_spread" in buckets.columns and buckets["policy_spread"].notna().any()

    # The last four 3-week averages, newest last; the final one is still filling.
    recent = [
        {"thru": str(row["thru"].date()), "spread": float(row["spread"]),
         "policy_spread": float(row["policy_spread"]) if has_policy and pd.notna(row.get("policy_spread")) else None,
         "partial": start == buckets.index[-1]}
        for start, row in buckets.tail(4).iterrows()
    ]

    detail = f"10y-3m spread {latest['spread']:+.2f}pp ({latest['date'].date()})"
    if not complete.empty:
        detail += f" | last full 3wk avg {complete['spread'].iloc[-1]:+.2f}pp"

    cuts_priced, cuts_since = None, None
    if has_policy and not complete.empty:
        if pd.isna(complete["policy_spread"].iloc[-1]):
            # Carried-over column has stopped advancing: better silent than stale.
            detail += " | 2y-FFR missing for the latest 3wk period"
        else:
            below = _buckets_below(complete)
            cuts_priced = below >= POLICY_SPREAD_NEG_BUCKETS
            cuts = "cuts priced in" if cuts_priced else "no cuts priced in"
            detail += f" | 2y-FFR below fed funds {below} straight 3wk periods, {cuts}"
            if cuts_priced:
                cuts_since = str(complete.index[-below].date())

    return SubSignal(
        "yield_curve",
        float(latest["spread"]),
        state,
        detail,
        passes=state in ("steep", "flat"),
        advisory=True,
        observed_at=str(latest["date"].date()),
        history=history.copy(),
        metrics={
            "policy_spread": None if "policy_spread" not in history or pd.isna(latest.get("policy_spread")) else float(latest["policy_spread"]),
            "cuts_priced": cuts_priced,
            "cuts_since": cuts_since,
            "buckets": recent,
            "inverted_share": float((history["spread"] < 0).mean()),
            "since": str(history["date"].iloc[0].date()),
        },
    )
