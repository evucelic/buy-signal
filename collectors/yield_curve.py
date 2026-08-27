"""Collect the daily FRED rate series behind the curve signal; cache to one CSV.

Two spreads, refreshed together: the 10y-3m curve (T10Y3M), and the policy spread,
2y Treasury minus effective fed funds (DGS2 - DFF, since FRED retired the ready-made
T2YFF series). The policy spread going negative means the bond market expects cuts.
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import pandas as pd

from collectors.fred import fetch_series_with_retries
from collectors.freshness import refreshed_today
from config import (
    FED_FUNDS_SERIES,
    FETCH_JITTER_SEC,
    TWO_YEAR_SERIES,
    YIELD_CURVE_CSV,
    YIELD_CURVE_SERIES,
)


def _fetch_policy_spread() -> tuple[pd.DataFrame | None, str | None]:
    """Daily 2y-minus-fed-funds spread from its two legs. Returns (data, error_message)."""
    two_year, error = fetch_series_with_retries(TWO_YEAR_SERIES, "2y Treasury")
    if two_year is None:
        return None, f"policy spread: {error}"

    fed_funds, error = fetch_series_with_retries(FED_FUNDS_SERIES, "Fed funds")
    if fed_funds is None:
        return None, f"policy spread: {error}"

    merged = two_year.merge(fed_funds, on="date", suffixes=("_2y", "_ffr"))
    merged["policy_spread"] = merged["value_2y"] - merged["value_ffr"]
    return merged[["date", "policy_spread"]], None


def _cached_policy_spread(filepath: Path) -> pd.DataFrame | None:
    """The policy spread already on disk, so a failed leg fetch can't erase years of it."""
    if not filepath.exists():
        return None

    try:
        cached = pd.read_csv(filepath, parse_dates=["date"])
    except Exception:
        return None  # truncated cache: the fresh write repairs it

    if "policy_spread" not in cached.columns:
        return None

    kept = cached[["date", "policy_spread"]].dropna(subset=["policy_spread"])
    return kept if not kept.empty else None


def update_yield_curve_data(filepath: Path | str = YIELD_CURVE_CSV) -> str | None:
    """Refresh the cached curve snapshot. Returns an error message, or None on success."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    history, error = fetch_series_with_retries(YIELD_CURVE_SERIES, "Yield curve")
    if history is None:
        return error or "unknown error"

    history = history.rename(columns={"value": "spread"})

    # The policy spread is advisory: if its legs are down, cache the curve anyway.
    policy, policy_error = _fetch_policy_spread()
    if policy is None or policy.empty:  # empty: both legs parsed but share no dates
        policy = _cached_policy_spread(filepath)
    if policy is not None:
        history = history.merge(policy, on="date", how="left")

    filepath.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(filepath, index=False)

    latest = history.iloc[-1]
    # Fed funds publishes a business day behind the Treasury series, so the last row of the
    # merge is normally an empty policy spread; report the last one that's actually there.
    policy_note = ""
    if "policy_spread" in history.columns:
        valid = history.dropna(subset=["policy_spread"])
        if not valid.empty:
            policy_note = (
                f", 2y-FFR {valid['policy_spread'].iloc[-1]:+.2f}pp "
                f"({valid['date'].iloc[-1].date()})"
            )
    print(
        f"Yield curve cache updated: {len(history)} days, latest "
        f"{latest['date'].date()} = {latest['spread']:+.2f}pp{policy_note}"
    )
    return policy_error


def yield_curve_history(filepath: Path | str = YIELD_CURVE_CSV) -> pd.DataFrame:
    """Return the daily spread history, ascending by date."""
    df = pd.read_csv(filepath, parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)


def _cache_has_policy_spread(filepath: Path) -> bool:
    try:
        return "policy_spread" in pd.read_csv(filepath, nrows=0).columns
    except Exception:
        return False


def should_refresh(filepath: Path | str = YIELD_CURVE_CSV) -> bool:
    """FRED updates once per business day, unless the cache lacks a column."""
    filepath = Path(filepath)
    return not refreshed_today(filepath) or not _cache_has_policy_spread(filepath)


if __name__ == "__main__":
    update_yield_curve_data()
    print(yield_curve_history().tail(6).to_string(index=False))
