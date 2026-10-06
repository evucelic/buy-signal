"""Score the CME FedWatch rate path across all three horizons."""

from __future__ import annotations

import pandas as pd

from buysignal.collectors.fed_rate import latest_fedwatch, _validate_probabilities
from buysignal.signals.base import SubSignal


def _meeting(df: pd.DataFrame, horizon: str) -> pd.Series:
    """Return exactly one meeting row for the requested horizon."""
    if "horizon" not in df.columns:
        raise ValueError("FedWatch horizons missing; refresh required")
    matches = df.loc[df["horizon"] == horizon]
    if len(matches) != 1:
        raise ValueError(f"FedWatch horizon {horizon} missing or duplicated; refresh required")
    return matches.iloc[0]


def _probabilities(meeting: pd.Series) -> tuple[float, float, float]:
    """Return Ease, No Change, and Hike probabilities from one meeting row."""
    ease = float(meeting["prob_ease"])
    no_change = float(meeting["prob_no_change"])
    hike = float(meeting["prob_hike"])
    _validate_probabilities((ease, no_change, hike))
    return ease, no_change, hike


def _format_meeting(label: str, meeting: pd.Series) -> str:
    """Format one meeting's probabilities for the signal detail."""
    ease, no_change, hike = _probabilities(meeting)
    return (
        f"{label} {meeting['meeting_date'].date()}: "
        f"ease {ease:.1%}, no change {no_change:.1%}, hike {hike:.1%}"
    )


def score(asof: pd.Timestamp | None = None) -> SubSignal:
    """Require no hike-leading horizon across the displayed rate path."""
    df = latest_fedwatch().sort_values("meeting_date").reset_index(drop=True)

    meetings = {
        "nearest": _meeting(df, "nearest"),
        "6m": _meeting(df, "six_month"),
        "1y": _meeting(df, "one_year"),
    }

    today = (asof if asof is not None else pd.Timestamp.now(tz="America/New_York").tz_localize(None)).normalize()
    if meetings["nearest"]["meeting_date"] < today:
        raise ValueError("FedWatch snapshot refers to a past meeting; refresh required")

    # A near-term pause is not a supportive path when later horizons price hikes.
    probabilities = {label: _probabilities(meeting) for label, meeting in meetings.items()}
    blocking = [label for label, (ease, hold, hike) in probabilities.items()
                if hike >= max(ease, hold)]
    ease, no_change, hike = _probabilities(meetings["nearest"])

    if blocking:
        state = "hiking"
    elif ease > max(no_change, hike):
        state = "cutting"
    elif no_change > max(ease, hike):
        state = "no_change"
    else:
        state = "flat"  # genuine tie between two or more of ease/no_change/hike

    detail = " | ".join(
        _format_meeting(label, meeting)
        for label, meeting in meetings.items()
    )

    return SubSignal(
        "fed_rate",
        max(p[2] for p in probabilities.values()),
        state,
        detail + " | " + (f"Rate-path gate failed: higher-rate risk leads or ties at {', '.join(blocking)}"
                          if blocking else "Rate-path gate passed: hikes do not lead at nearest, 6m or 1y")
        + "; probabilities compare with the current target range, not the previous meeting",
        passes=not blocking,
    )
