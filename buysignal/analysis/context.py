"""Where a reading sits in its own past: descriptive context, never a forecast.

Every figure here uses only the observations handed in, so a historical snapshot is described
with the history it had at the time. A share of past days is a frequency, not a probability of
any future outcome.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_OBSERVATIONS = 120  # below this a percentile says more about the sample than the reading


def _clean(values) -> np.ndarray:
    array = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    return array[np.isfinite(array)]


def share_at_or_below(values, current: float) -> float | None:
    """Fraction of past observations at or below the current one."""
    array = _clean(values)
    if len(array) < MIN_OBSERVATIONS or current is None or not np.isfinite(current):
        return None
    return float((array <= current).mean())


def usual_range(values) -> tuple[float, float] | None:
    """The middle half of past observations (25th to 75th percentile)."""
    array = _clean(values)
    if len(array) < MIN_OBSERVATIONS:
        return None
    low, high = np.percentile(array, [25, 75])
    return float(low), float(high)


def describe(values, current: float, dates=None) -> dict:
    """Percentile, usual range and the first observation date, as plain JSON-safe facts."""
    facts = {"percentile": share_at_or_below(values, current), "usual_low": None, "usual_high": None, "since": None}
    band = usual_range(values)
    if band:
        facts["usual_low"], facts["usual_high"] = band
    if dates is not None and len(dates):
        facts["since"] = str(pd.Timestamp(pd.Series(dates).iloc[0]).date())
    return facts
