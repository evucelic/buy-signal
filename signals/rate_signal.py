"""Score the one-year CME FedWatch rate outlook."""

from __future__ import annotations

import pandas as pd

from collectors.fed_rate import latest_fedwatch
from signals.base import SubSignal


def _meeting(df: pd.DataFrame, horizon: str) -> pd.Series:
    """Return the meeting row for a horizon, or the latest row if missing."""
    if "horizon" in df.columns:
        matches = df.loc[df["horizon"] == horizon]
        if not matches.empty:
            return matches.iloc[0]
    return df.iloc[-1]


def _probabilities(meeting: pd.Series) -> tuple[float, float, float]:
    """Return Ease, No Change, and Hike probabilities from one meeting row."""
    ease = float(meeting.get("prob_ease", 0.0))
    no_change = float(meeting.get("prob_no_change", 0.0))
    hike = float(meeting.get("prob_hike", 0.0))
    return ease, no_change, hike


def _format_meeting(label: str, meeting: pd.Series) -> str:
    """Format one meeting's probabilities for the signal detail."""
    ease, no_change, hike = _probabilities(meeting)
    return (
        f"{label} {meeting['meeting_date'].date()}: "
        f"ease {ease:.1%}, no change {no_change:.1%}, hike {hike:.1%}"
    )


def score() -> SubSignal:
    """Return the FedWatch sub-signal for the one-year meeting."""
    df = latest_fedwatch().sort_values("meeting_date").reset_index(drop=True)

    meetings = {
        "nearest": _meeting(df, "nearest"),
        "6m": _meeting(df, "six_month"),
        "1y": _meeting(df, "one_year"),
    }

    ease, no_change, hike = _probabilities(meetings["1y"])

    if hike > max(ease, no_change):
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
        hike,
        state,
        detail,
        passes=state != "hiking",
    )
