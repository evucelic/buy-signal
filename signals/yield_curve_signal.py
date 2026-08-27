"""Score indicator #7: Treasury yield curve, advisory macro-risk context.

Advisory only: an inverted curve is an early recession warning that can lead the market by
many months, so it rides alongside the checklist (advisory=True) without gating it.

Two spreads share the table, both read in complete 3-week buckets so day-to-day noise can't
flip anything. The 10y-3m curve classifies the regime. The 2y-minus-fed-funds policy spread
only reports: below zero means the bond market expects cuts, which has preceded soft-landing
cutting cycles (1995, 1998, 2019) as often as recessionary ones (1989, 2001, 2007), so it
flags a policy shift ahead rather than a direction for equities.
"""

from __future__ import annotations

import pandas as pd

from collectors.yield_curve import yield_curve_history
from config import (
    POLICY_SPREAD_NEG_BUCKETS,
    YIELD_CURVE_BUCKET_DAYS,
    YIELD_CURVE_DEEP_INVERSION,
    YIELD_CURVE_STEEP,
)
from signals.base import SubSignal, format_table

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
    """Per-bucket means of both spreads, plus the last date each bucket actually holds.

    Buckets are fixed width from the start of the series, so the rows don't shift under a
    refresh. The final one is still filling, and every classification excludes it.
    """
    work = history.copy()
    work["thru"] = work["date"]

    aggregation = {"spread": "mean", "thru": "max"}
    if "policy_spread" in work.columns:
        aggregation["policy_spread"] = "mean"

    return work.set_index("date").resample(_BUCKET).agg(aggregation).dropna(subset=["spread"])


def _buckets_below(complete: pd.DataFrame) -> int:
    """How many consecutive complete buckets, ending with the latest, averaged below fed funds.

    A gap counts as a break, not as a skip: the collector keeps the last known policy column
    when a leg is down, so dropping empties would read stale buckets as an unbroken run.
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
    """Return the yield-curve advisory sub-signal from the cached spread history."""
    history = yield_curve_history()
    latest = history.iloc[-1]

    buckets = _bucketed(history)
    complete = buckets.iloc[:-1]  # the newest bucket is still filling

    state = _classify(complete["spread"]) if not complete.empty else "flat"
    has_policy = "policy_spread" in buckets.columns and buckets["policy_spread"].notna().any()

    recent = buckets.tail(4)
    headers = ["Thru", "10y-3m"]
    rows = [
        [f"{row['thru'].date()}{'*' if start == buckets.index[-1] else ''}", f"{row['spread']:+.2f}pp"]
        for start, row in recent.iterrows()
    ]
    if has_policy:
        headers.append("2y-FFR")
        for row, (_, bucket) in zip(rows, recent.iterrows()):
            value = bucket.get("policy_spread")
            row.append(f"{value:+.2f}pp" if pd.notna(value) else "n/a")
    table = format_table(headers, rows)

    detail = f"10y-3m spread {latest['spread']:+.2f}pp ({latest['date'].date()})"
    if not complete.empty:
        detail += f" | last full 3wk avg {complete['spread'].iloc[-1]:+.2f}pp"

    footer = None
    if has_policy and not complete.empty:
        if pd.isna(complete["policy_spread"].iloc[-1]):
            # Carried-over column has stopped advancing: better silent than stale.
            detail += " | 2y-FFR missing for the latest 3wk period"
        else:
            below = _buckets_below(complete)
            cuts = "cuts priced in" if below >= POLICY_SPREAD_NEG_BUCKETS else "no cuts priced in"
            detail += f" | 2y-FFR below fed funds {below} straight 3wk periods, {cuts}"
            if below >= POLICY_SPREAD_NEG_BUCKETS:
                since = complete.index[-below]
                footer = f"🔻 Cuts priced in since {since.date()}. First cut historically 4-20mo out."
    detail += f" | {_BLURBS[state]}"

    return SubSignal(
        "yield_curve",
        float(latest["spread"]),
        state,
        detail,
        passes=state in ("steep", "flat"),
        table=table,
        advisory=True,
        footer=footer,
    )
