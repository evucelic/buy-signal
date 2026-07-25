"""Fetch hourly closes for SPY/^IXIC/^DJI from yfinance; cache to CSV, merging new bars by timestamp (UTC).

Hourly (same cadence as vix.py) so "daily" % change reflects the live intraday price, not just
the prior finalized daily bar.
"""

from pathlib import Path
import random
import time

import pandas as pd
import yfinance as yf

from config import (
    FETCH_JITTER_SEC,
    INDEX_TICKERS,
    MARKET_CSV,
    MARKET_INTERVAL,
    MARKET_LOOKBACK_DAYS,
    MARKET_MONTHLY_LOOKBACK_DAYS,
    MARKET_WEEKLY_LOOKBACK_DAYS,
)

_RECENT_WINDOW = "5d"  # a few days to have some leeway with failing to fetch


def _download_closes(period: str) -> pd.DataFrame | None:
    """Hourly closes for all INDEX_TICKERS as a UTC-indexed DataFrame, or None if empty."""
    time.sleep(random.uniform(*FETCH_JITTER_SEC))   # jitter so hourly pulls aren't periodic
    data = yf.download(
        list(INDEX_TICKERS.values()),
        period=period,
        interval=MARKET_INTERVAL,
        auto_adjust=False,
        progress=False,
    )
    if data is None or data.empty:
        return None
    close = data["Close"].dropna(how="all")
    close = close.rename(columns={ticker: name for name, ticker in INDEX_TICKERS.items()})
    close.index = pd.to_datetime(close.index, utc=True)
    return close if not close.empty else None


def refresh_market_cache(filepath: Path = MARKET_CSV) -> bool:
    """Fetch recent index closes and merge into the cache (full history on first run)."""
    filepath = Path(filepath)
    first_run = not filepath.exists()

    new = _download_closes(f"{MARKET_LOOKBACK_DAYS}d" if first_run else _RECENT_WINDOW)
    if new is None:
        print("Market index download returned no data; cache unchanged.")
        return False

    if first_run:
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        old = pd.read_csv(filepath, index_col=0)  # fetch the old data from cache (CSV file on machine)
        old.index = pd.to_datetime(old.index, utc=True)
        new = pd.concat([old, new])  # concat the new and old dataframes
        new = new[~new.index.duplicated(keep="last")].sort_index()  # deduplicate indexes if they overlap

    new.to_csv(filepath)
    print(f"Market index cache updated: {len(new)} rows, latest {new.index[-1].date()}")
    return True


def latest_changes(filepath: Path = MARKET_CSV) -> dict[str, dict[str, float]] | None:
    """Daily/weekly/monthly % change (fractions) per index, from the cache without fetching."""
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    df = pd.read_csv(filepath, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    # Index bars only cover regular NYSE hours (13:30-20:00 UTC), so a UTC calendar date is
    # always the same as the ET trading date — grouping by it needs no timezone conversion.
    # Today's (possibly partial) date is its own last row, so its "close" is the live price.
    daily = df.groupby(df.index.date).last()

    changes = {}
    for name in INDEX_TICKERS:
        closes = daily[name].dropna()
        latest = closes.iloc[-1]
        changes[name] = {
            "daily": latest / closes.iloc[-2] - 1,
            "weekly": latest / closes.iloc[-1 - MARKET_WEEKLY_LOOKBACK_DAYS] - 1,
            "monthly": latest / closes.iloc[-1 - MARKET_MONTHLY_LOOKBACK_DAYS] - 1,
        }
    return changes


def get_latest_market_changes(filepath: Path = MARKET_CSV) -> dict[str, dict[str, float]]:
    """Refresh the cache and return the current daily/weekly/monthly % changes."""
    filepath = Path(filepath)
    first_run = not filepath.exists()
    refreshed = refresh_market_cache(filepath)
    changes = latest_changes(filepath)
    if changes is None:
        if first_run:
            raise RuntimeError(f"Unable to load market data: download failed and no cache exists at {filepath}")
        if not refreshed:
            raise RuntimeError(f"Unable to load market data from cache at {filepath}")
        raise RuntimeError(f"Unable to load market data from cache at {filepath}")
    return changes


if __name__ == "__main__":
    refresh_market_cache()
