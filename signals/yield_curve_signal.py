"""Score indicator #7: 10y-3m Treasury yield curve, advisory macro-risk context.

Advisory only: an inverted curve is an early recession warning that can lead the market by
many months, so it rides alongside the checklist (advisory=True) without gating it. Regimes
are classified from complete calendar-month averages so one-day moves can't flip the state.
"""

from __future__ import annotations

import pandas as pd

from collectors.yield_curve import yield_curve_history
from config import YIELD_CURVE_DEEP_INVERSION, YIELD_CURVE_STEEP
from signals.base import SubSignal, format_table

_BLURBS = {
    "deep_inversion": "deep persistent inversion, high recession risk",
    "inverted": "restrictive late-cycle environment",
    "flat": "neutral or transitional environment",
    "steep": "supportive growth/liquidity regime",
}


def _classify(complete_months: pd.Series) -> str:
    """Regime from complete-month averages (last = most recent). Needs at least one month."""
    last = complete_months.iloc[-1]
    if len(complete_months) >= 2 and (complete_months.iloc[-2:] <= YIELD_CURVE_DEEP_INVERSION).all():
        return "deep_inversion"
    if last < 0:
        return "inverted"
    if last < YIELD_CURVE_STEEP:
        return "flat"
    return "steep"


def score() -> SubSignal:
    """Return the yield-curve advisory sub-signal from the cached T10Y3M history."""
    history = yield_curve_history()
    latest = history.iloc[-1]

    monthly = history.groupby(history["date"].dt.to_period("M"))["spread"].mean()
    current_month = latest["date"].to_period("M")
    complete = monthly[monthly.index < current_month]

    state = _classify(complete) if not complete.empty else "flat"

    recent = monthly.tail(4)
    rows = [
        [f"{month}{'*' if month == current_month else ''}", f"{avg:+.2f}pp"]
        for month, avg in recent.items()
    ]
    table = format_table(["Month", "Avg 10y-3m"], rows)

    detail = (
        f"10y-3m spread {latest['spread']:+.2f}pp ({latest['date'].date()}) | "
        f"last full month avg {complete.iloc[-1]:+.2f}pp | {_BLURBS[state]}"
        if not complete.empty
        else f"10y-3m spread {latest['spread']:+.2f}pp ({latest['date'].date()}) | {_BLURBS[state]}"
    )

    return SubSignal(
        "yield_curve",
        float(latest["spread"]),
        state,
        detail,
        passes=state in ("steep", "flat"),
        table=table,
        advisory=True,
    )
