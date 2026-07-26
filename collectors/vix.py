"""Fetch ^VIX intraday bars from yfinance; cache to CSV, merging new bars by timestamp (UTC)."""

from pathlib import Path
import random
import time

import pandas as pd
import yfinance as yf

from config import FETCH_JITTER_SEC, VIX_CSV, VIX_INTERVAL, VIX_LOOKBACK_DAYS, VIX_TICKER

_RECENT_WINDOW = "5d" # 5 days to have some leeway with failing to fetch


def _download_close(period: str) -> pd.Series | None:
    """VIX closes for `period` as a UTC-indexed Series, or None if empty."""
    time.sleep(random.uniform(*FETCH_JITTER_SEC))   # jitter so hourly pulls aren't periodic
    data = yf.download(
        VIX_TICKER,
        period=period,
        interval=VIX_INTERVAL,
        auto_adjust=False,
        progress=False,
        multi_level_index=False,
    )
    if data is None or data.empty:
        return None
    close = data["Close"].dropna()
    close.name = "Close"
    close.index = pd.to_datetime(close.index, utc=True)
    return close if not close.empty else None


def refresh_vix_cache(filepath: Path = VIX_CSV) -> bool:
    """Fetch recent VIX and merge into the cache (full history on first run)."""
    filepath = Path(filepath)
    first_run = not filepath.exists()

    new = _download_close(f"{VIX_LOOKBACK_DAYS}d" if first_run else _RECENT_WINDOW)
    if new is None:
        print("VIX download returned no data; cache unchanged.")
        return False

    if first_run:
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        old = pd.read_csv(filepath, index_col=0)["Close"] # fetch the old data from cache (CSV file on machine)
        old.index = pd.to_datetime(old.index, utc=True)
        new = pd.concat([old, new]) # concat the new and old dataframes
        new = new[~new.index.duplicated(keep="last")].sort_index() # deduplicate indexes if they overlap

    new.to_csv(filepath)
    print(f"VIX cache updated: {len(new)} rows, latest {new.index[-1]} = {new.iloc[-1]:.2f}")
    return True


def load_latest_cached_vix(filepath: Path = VIX_CSV) -> float | None:
    """Most recent cached VIX close, without fetching. Returns the last row of the VIX CSV file"""
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    df = pd.read_csv(filepath, index_col=0)
    if df.empty or "Close" not in df.columns:
        return None
    return float(df["Close"].iloc[-1])


def vix_change_pct(filepath: Path = VIX_CSV) -> float | None:
    """Latest VIX close's % change vs the prior trading day's close, or None if too little history."""
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    df = pd.read_csv(filepath, index_col=0)
    if df.empty or "Close" not in df.columns:
        return None
    df.index = pd.to_datetime(df.index, utc=True)
    daily = df["Close"].groupby(df.index.date).last().dropna()
    if len(daily) < 2:
        return None
    return daily.iloc[-1] / daily.iloc[-2] - 1


def get_latest_vix(filepath: Path = VIX_CSV) -> float:
    """Refresh the cache and return the current (last recorded in the CSV file) VIX close."""
    filepath = Path(filepath)
    first_run = not filepath.exists()
    refreshed = refresh_vix_cache(filepath)
    vix = load_latest_cached_vix(filepath)
    if vix is None:
        if first_run:
            raise RuntimeError(f"Unable to load VIX: download failed and no cache exists at {filepath}")
        if not refreshed:
            raise RuntimeError(f"Unable to load VIX from cache at {filepath}")
        raise RuntimeError(f"Unable to load VIX from cache at {filepath}")
    return vix


if __name__ == "__main__":
    refresh_vix_cache()
