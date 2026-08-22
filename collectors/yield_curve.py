"""Collect the US Treasury 10y-3m yield spread (FRED T10Y3M); cache to CSV."""

from __future__ import annotations

import random
import time
from pathlib import Path

import pandas as pd

from collectors.fred import fetch_series_with_retries
from collectors.freshness import refreshed_today
from config import FETCH_JITTER_SEC, YIELD_CURVE_CSV, YIELD_CURVE_SERIES


def update_yield_curve_data(filepath: Path | str = YIELD_CURVE_CSV) -> str | None:
    """Refresh the cached T10Y3M snapshot. Returns an error message, or None on success."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    history, error = fetch_series_with_retries(YIELD_CURVE_SERIES, "Yield curve")
    if history is None:
        return error or "unknown error"

    history = history.rename(columns={"value": "spread"})
    filepath.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(filepath, index=False)

    latest = history.iloc[-1]
    print(
        f"Yield curve cache updated: {len(history)} days, latest "
        f"{latest['date'].date()} = {latest['spread']:+.2f}pp"
    )
    return None


def yield_curve_history(filepath: Path | str = YIELD_CURVE_CSV) -> pd.DataFrame:
    """Return the daily 10y-3m spread history, ascending by date."""
    df = pd.read_csv(filepath, parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)


def should_refresh(filepath: Path | str = YIELD_CURVE_CSV) -> bool:
    """FRED updates the series once per business day; once-a-day is enough."""
    return not refreshed_today(filepath)


if __name__ == "__main__":
    update_yield_curve_data()
    print(yield_curve_history().tail(6).to_string(index=False))
