"""Credit context: how much more interest US companies pay than the Treasury (Baa minus 10-year)."""

from __future__ import annotations

import pandas as pd

from buysignal.analysis.context import describe
from buysignal.collectors.credit import credit_history
from buysignal.config import (
    CREDIT_CHANGE_SESSIONS, CREDIT_MAX_OBSERVATION_AGE_DAYS, CREDIT_STRESS_MIN_OBS,
    CREDIT_STRESS_PERCENTILE, CREDIT_STRESS_WINDOW,
)
from buysignal.signals.base import SubSignal


def score(asof: pd.Timestamp | None = None) -> SubSignal:
    """Calm below the stress line, stressed at or above it: the 80th percentile of the spread's last ten years."""
    history = credit_history()
    today = asof if asof is not None else pd.Timestamp.now(tz="America/New_York").tz_localize(None)
    if today.tzinfo is not None:
        today = today.tz_convert("America/New_York").tz_localize(None)
    latest = history.iloc[-1]
    age = (today.normalize() - latest.date.normalize()).days
    if age < 0 or age > CREDIT_MAX_OBSERVATION_AGE_DAYS:
        raise ValueError(f"Credit observation {latest.date.date()} is outside the freshness budget")
    window = history.spread.tail(CREDIT_STRESS_WINDOW)
    if len(window) < CREDIT_STRESS_MIN_OBS:
        raise ValueError(f"Credit history has {len(window)} observations; the stress line needs {CREDIT_STRESS_MIN_OBS}")
    spread = float(latest.spread)
    stress = float(window.quantile(CREDIT_STRESS_PERCENTILE))
    calm = spread < stress
    changes = {}
    for n in CREDIT_CHANGE_SESSIONS:
        changes[n] = (spread - float(history.spread.iloc[-1 - n])) * 100 if len(history) > n else None
    detail = (
        f"Baa corporate yield minus 10-year Treasury {spread:.2f} points; "
        f"stress line {stress:.2f} ({CREDIT_STRESS_PERCENTILE:.0%} of the last {len(window)} observations sit below it)"
    )
    return SubSignal(
        "credit", spread, "calm" if calm else "stressed", detail, passes=calm, advisory=True,
        observed_at=str(latest.date.date()), history=history.copy(),
        metrics={"spread": spread, "stress": stress, "changes_bp": changes,
                 **describe(history.spread, spread, history.date)},
    )
