"""Fetch ^VIX intraday bars from yfinance; cache to CSV, merging new bars by timestamp (UTC)."""

import logging
import math
from pathlib import Path
import random
import time

import pandas as pd
import yfinance as yf

from buysignal.config import FETCH_JITTER_SEC, VIX_CSV, VIX_INTERVAL, VIX_LOOKBACK_DAYS, VIX_TICKER, LIVE_CACHE_TTL_SEC, YAHOO_TIMEOUT_SEC
from buysignal.resources import FORCE_REFRESH
from buysignal.collectors.cache import write_csv

log = logging.getLogger(__name__)

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
        threads=False, timeout=YAHOO_TIMEOUT_SEC,
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
        log.warning("VIX download returned no data; cache unchanged.")
        return False

    if first_run:
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        old = pd.read_csv(filepath, index_col=0)["Close"] # fetch the old data from cache (CSV file on machine)
        old.index = pd.to_datetime(old.index, utc=True)
        new = pd.concat([old, new]) # concat the new and old dataframes
        new = new[~new.index.duplicated(keep="last")].sort_index() # deduplicate indexes if they overlap

    # Keep the provider-supported intraday window bounded on disk.
    cutoff = new.index[-1] - pd.Timedelta(days=VIX_LOOKBACK_DAYS)
    new = new.loc[new.index >= cutoff]
    write_csv(new.to_frame(name="Close"), filepath, index=True)
    log.info("VIX cache updated: %d rows, latest %s = %.2f", len(new), new.index[-1], new.iloc[-1])
    return True


def load_latest_cached_vix(filepath: Path = VIX_CSV) -> float | None:
    """Most recent cached VIX close, without fetching. Returns the last row of the VIX CSV file"""
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    df = pd.read_csv(filepath, index_col=0)
    if df.empty or "Close" not in df.columns:
        return None
    value = float(df["Close"].iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


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


def _vix_session_is_current(filepath: Path, now: pd.Timestamp) -> bool:
    from buysignal.schedule.market_hours import _NYSE
    local = now.tz_convert("America/Chicago")
    schedule = _NYSE.schedule(start_date=(now-pd.Timedelta(days=14)).date(), end_date=local.date())
    dates = schedule.index
    # VIX opens before equities. Allow one hour for the first hourly observation.
    if local.hour < 3:
        dates = dates[dates.date < local.date()]
    if len(dates) == 0:
        return False
    try:
        history = daily_history(filepath)
        observed = history.date.iloc[-1].date()
        return dates[-1].date() <= observed <= local.date()
    except (OSError, ValueError, KeyError, IndexError):
        return False


def get_latest_vix(filepath: Path = VIX_CSV, now: pd.Timestamp | None = None) -> float:
    filepath = Path(filepath)
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    recent = filepath.exists() and time.time() - filepath.stat().st_mtime < LIVE_CACHE_TTL_SEC
    source_current = _vix_session_is_current(filepath, now)
    if FORCE_REFRESH.get() or not recent or not source_current:
        refresh_vix_cache(filepath)
    vix = load_latest_cached_vix(filepath)
    if vix is None:
        raise RuntimeError(f"Unable to load VIX: download failed and no usable cache exists at {filepath}")
    if not _vix_session_is_current(filepath, now):
        raise RuntimeError("VIX observation predates the expected session")
    return vix


def daily_history(filepath: Path | str = VIX_CSV) -> pd.DataFrame:
    """Daily last observations for charting and fear context, without fetching."""
    frame = pd.read_csv(filepath, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True).tz_convert("America/New_York")
    series = frame["Close"].groupby(frame.index.date).last().dropna()
    return pd.DataFrame({"date": pd.to_datetime(series.index), "vix": series.to_numpy()})


if __name__ == "__main__":
    from buysignal import logs

    logs.configure()
    refresh_vix_cache()
