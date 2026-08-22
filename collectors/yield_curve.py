"""Collect the US Treasury 10y-3m yield spread (FRED T10Y3M); cache to CSV.

FRED's fredgraph.csv endpoint serves the full daily history without an API key. Market
holidays come through as blank observations (NaN after read_csv), so they're dropped.
"""

from __future__ import annotations

import random
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from collectors.freshness import refreshed_today
from config import (
    FETCH_JITTER_SEC,
    FRED_GRAPH_CSV_URL,
    SCRAPE_BACKOFF_SEC,
    SCRAPE_RETRIES,
    YIELD_CURVE_CSV,
    YIELD_CURVE_SERIES,
)


def _fetch_history() -> pd.DataFrame:
    """Download and parse the full T10Y3M daily history, ascending by date."""
    resp = requests.get(FRED_GRAPH_CSV_URL, params={"id": YIELD_CURVE_SERIES}, timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), parse_dates=["observation_date"])
    df = df.rename(columns={"observation_date": "date", YIELD_CURVE_SERIES: "spread"})
    return df.dropna(subset=["spread"]).sort_values("date").reset_index(drop=True)


def _fetch_with_retries() -> tuple[pd.DataFrame | None, str | None]:
    """Fetch the spread history with retry/backoff. Returns (data, error_message)."""
    last_error = None
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _fetch_history(), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 == SCRAPE_RETRIES:
                print(f"Yield curve fetch failed after {SCRAPE_RETRIES} tries ({last_error}); cache unchanged.")
                return None, last_error

            delay = SCRAPE_BACKOFF_SEC * 2**attempt + random.uniform(0, 1)
            print(f"Yield curve attempt {attempt + 1} failed ({last_error}); retry in {delay:.0f}s")
            time.sleep(delay)

    return None, last_error


def update_yield_curve_data(filepath: Path | str = YIELD_CURVE_CSV) -> str | None:
    """Refresh the cached T10Y3M snapshot. Returns an error message, or None on success."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    history, error = _fetch_with_retries()
    if history is None:
        return error or "unknown error"

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
