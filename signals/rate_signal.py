# TODO implement the  changes listed below
"""Score indicator #2: Fed rate trajectory from CME FedWatch.

Current plan for this signal:
- use the meeting closest to one year from the current date
- if two meetings are equally far away, pick the later meeting
- read the meeting's Ease / No Change / Hike probabilities directly from the
  FedWatch Probabilities table
- treat those probabilities as the diagnostic output for this signal

The final checklist pass/fail rule can stay separate from the collector:
- pass when the selected meeting is not hike-heavy
- fail when hike probability dominates
"""
import numpy as np
import pandas as pd

from collectors.fed_rate import latest_fedwatch
from config import FED_EASE_BPS, FED_HIKE_BPS, FEDWATCH_HORIZON_MONTHS
from signals.base import SubSignal


def score() -> SubSignal:
    df = latest_fedwatch().sort_values("meeting_date").reset_index(drop=True)

    cutoff = df["meeting_date"].iloc[0] + pd.DateOffset(months=FEDWATCH_HORIZON_MONTHS)
    df = df[df["meeting_date"] <= cutoff]

    expected = df["expected_rate"].to_numpy()
    # Anchor to today's actual rate (FRED); fall back to nearest meeting for old caches.
    current = float(df["current_rate"].iloc[0]) if "current_rate" in df.columns else expected[0]
    delta = expected - current
    weights = np.arange(1, len(expected) + 1)
    tilt_bps = 100 * np.sum(weights * delta) / np.sum(weights)

    if tilt_bps <= FED_EASE_BPS:
        state = "cutting"
    elif tilt_bps >= FED_HIKE_BPS:
        state = "hiking"
    else:
        state = "flat"

    # Per-meeting expected move (bps) vs the current rate.
    moves = ", ".join(
        f"{d.strftime('%b%y')} {100 * (e - current):+.0f}"
        for d, e in zip(df["meeting_date"], expected)
    )
    detail = f"tilt {tilt_bps:+.1f}bp vs {current:.2f}% now | moves: {moves}"
    return SubSignal("fed_rate", tilt_bps, state, detail, passes=state != "hiking")
