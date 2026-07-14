"""Fetch ^VIX intraday bars from yfinance; cache to CSV, merging new bars by timestamp.

Fetch-and-cache only — the caller (runner.py) decides when to run. Idempotent.
"""

import os
import random
import time

import pandas as pd
import yfinance as yf

from config import FETCH_JITTER_SEC, VIX_CSV, VIX_INTERVAL, VIX_LOOKBACK_DAYS, VIX_TICKER

_RECENT_WINDOW = "5d"   # routine updates pull a short window and merge (overlap deduped)


def _download_close(period):
    """VIX closes for `period` as a UTC-indexed Series, or None if empty."""
    time.sleep(random.uniform(*FETCH_JITTER_SEC))   # jitter so hourly pulls aren't periodic
    data = yf.download(
        VIX_TICKER, period=period, interval=VIX_INTERVAL,
        auto_adjust=False, progress=False, multi_level_index=False,
    )
    if data is None or data.empty:
        return None
    close = data["Close"].dropna()
    close.name = "Close"
    # UTC so the CSV round-trips across DST (mixed offsets break the index).
    close.index = pd.to_datetime(close.index, utc=True)
    return close if not close.empty else None


def update_vix_data(filepath=VIX_CSV):
    """Fetch recent VIX and merge into the cache (full history on first run)."""
    filepath = str(filepath)
    first_run = not os.path.exists(filepath)

    new = _download_close(f"{VIX_LOOKBACK_DAYS}d" if first_run else _RECENT_WINDOW)
    if new is None:
        print("VIX download returned no data; cache unchanged.")
        return

    if first_run:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)   # only needed once
    else:
        old = pd.read_csv(filepath, index_col=0)["Close"]
        old.index = pd.to_datetime(old.index, utc=True)
        new = pd.concat([old, new])
        new = new[~new.index.duplicated(keep="last")].sort_index()

    new.to_csv(filepath)
    print(f"VIX cache updated: {len(new)} rows, latest {new.index[-1]} = {new.iloc[-1]:.2f}")


def latest_vix(filepath=VIX_CSV):
    """Most recent cached VIX close, without fetching."""
    return float(pd.read_csv(filepath, index_col=0)["Close"].iloc[-1])


def fetch_vix(filepath=VIX_CSV):
    """Refresh the cache and return the current VIX close."""
    update_vix_data(filepath)
    return latest_vix(filepath)


if __name__ == "__main__":
    update_vix_data()
