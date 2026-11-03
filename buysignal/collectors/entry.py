"""Adjusted daily index history for drawdown, selloff intensity and long-term trend.

The old hourly market.csv stays readable by existing tools. This separate daily
cache bootstraps the available history without mixing raw hourly and adjusted daily prices.
Yahoo's latest daily bar may be in progress; completed-session baselines exclude it.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
import random
import time

import numpy as np
import pandas as pd
import yfinance as yf

from buysignal.collectors.cache import write_csv
from buysignal.config import INDEX_TICKERS, OPTIONAL_INDEX_TICKERS, MARKET_DAILY_CSV, MARKET_DAILY_PERIOD, MARKET_BOOTSTRAP_PERIOD, FETCH_JITTER_SEC, LIVE_CACHE_TTL_SEC, YAHOO_TIMEOUT_SEC
from buysignal.resources import FORCE_REFRESH
from buysignal.schedule.market_hours import _NYSE

log = logging.getLogger(__name__)


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    index = pd.to_datetime(frame.index)
    if index.tz is not None:
        index = index.tz_convert("America/New_York").tz_localize(None)
    frame.index = index.normalize()
    frame.index.name = "date"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if frame.empty or not set(INDEX_TICKERS).issubset(frame.columns):
        raise ValueError("Daily index history is empty or missing an index")
    if frame.index.isna().any() or frame["SPY"].dropna().empty:
        raise ValueError("Daily history is missing SPY observations or dates")
    for name in OPTIONAL_INDEX_TICKERS:
        if name in frame:
            numeric = pd.to_numeric(frame[name], errors="coerce")
            valid = numeric.dropna()
            if valid.empty or not np.isfinite(valid).all() or (valid <= 0).any():
                frame = frame.drop(columns=[name])
                log.warning("Optional index %s unavailable or invalid", name)
    columns = [name for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS) if name in frame]
    frame = frame[columns].apply(pd.to_numeric, errors="raise")
    values = frame.to_numpy(dtype=float)
    present = ~np.isnan(values)
    if not np.isfinite(values[present]).all() or (values[present] <= 0).any():
        raise ValueError("Daily index prices must be positive and finite")
    return frame.dropna(how="all")


def _merge_adjusted(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Rebase the retained prefix using two stable, completed overlap observations."""
    result = old.copy()
    for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS):
        if name not in old or name not in new:
            continue
        common = old[name].dropna().index.intersection(new[name].dropna().index)
        # The last bar may still be changing. Never use it as an adjustment anchor.
        common = common[common < max(old.index[-1], new.index[-1])]
        if len(common) < 2:
            if name in OPTIONAL_INDEX_TICKERS:
                # Reconcile this optional series on the next daily full request.
                new = new.drop(columns=[name])
                continue
            raise ValueError("Insufficient completed overlap for adjusted-price reconciliation")
        anchors = common[:2]
        ratios = new.loc[anchors, name] / old.loc[anchors, name]
        if not np.allclose(ratios, ratios.iloc[0], rtol=1e-5, atol=1e-8):
            raise ValueError("Nonuniform price revision; full reconciliation required")
        result[name] *= float(ratios.iloc[0])
    # A failed ticker must not silently turn into today's partial snapshot.
    if new.loc[new.SPY.notna(), list(INDEX_TICKERS)].isna().any().any():
        raise ValueError("Incremental index download is incomplete")
    return _normalize(pd.concat([result.loc[result.index < new.index[0]], new]))


def refresh_entry_history(filepath: Path | str = MARKET_DAILY_CSV) -> bool:
    """Daily full adjustment reconciliation; small incremental requests in between."""
    path = Path(filepath)
    metadata_path = path.with_suffix(".meta.json")
    today = pd.Timestamp.now(tz="America/New_York").date().isoformat()
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, ValueError):
        metadata = {}
    try:
        old = entry_history(path)
    except (OSError, ValueError):
        old = None
    bootstrap = old is None or metadata.get("bootstrap_scope") != MARKET_BOOTSTRAP_PERIOD
    full = (bootstrap or FORCE_REFRESH.get() or metadata.get("full_refresh_date") != today
            or metadata.get("optional_indexes") != list(OPTIONAL_INDEX_TICKERS))
    try:
        def download(period):
            time.sleep(random.uniform(*FETCH_JITTER_SEC))
            raw = yf.download(
                [*INDEX_TICKERS.values(), *OPTIONAL_INDEX_TICKERS.values()], period=period, interval="1d", auto_adjust=True,
                progress=False, threads=False, timeout=YAHOO_TIMEOUT_SEC,
            )
            if raw is None or raw.empty:
                raise ValueError("Daily index download returned no data")
            return _normalize(raw["Close"].rename(columns={v: k for k, v in (INDEX_TICKERS | OPTIONAL_INDEX_TICKERS).items()}))
        new = download(MARKET_BOOTSTRAP_PERIOD if bootstrap else MARKET_DAILY_PERIOD if full else "5d")
        if full:
            frame = new
            if old is not None and old.index[0] < new.index[0]:
                try:
                    frame = _merge_adjusted(old, new)
                except ValueError:
                    log.warning("Older price prefix requires a full-provider reconciliation")
                    frame = download(MARKET_BOOTSTRAP_PERIOD)
        else:
            try:
                frame = _merge_adjusted(old, new)
            except ValueError:
                frame = download(MARKET_BOOTSTRAP_PERIOD)
                full = True
        if len(frame.dropna(subset=["SPY"])) < 220:
            raise ValueError("Daily index download contains fewer than 220 SPY sessions")
        if frame.loc[frame.SPY.notna(), list(INDEX_TICKERS)].tail(22).isna().any().any():
            raise ValueError("Daily index download has missing recent index observations")
        if old is not None and frame.index[-1] < old.index[-1]:
            raise ValueError("Daily index download moved backwards")
        write_csv(frame, path, index=True)
        if full:
            # Metadata only controls bandwidth. A failed metadata write merely causes
            # another full reconciliation; the data file was already published atomically.
            temporary = metadata_path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps({"full_refresh_date": today, "bootstrap_scope": MARKET_BOOTSTRAP_PERIOD,
                                                  "optional_indexes": list(OPTIONAL_INDEX_TICKERS)}))
                temporary.replace(metadata_path)
            except OSError:
                temporary.unlink(missing_ok=True)
        return True
    except Exception as exc:
        log.warning("Daily index refresh failed (%s: %s); cache unchanged", type(exc).__name__, exc)
        return False


def entry_history(filepath: Path | str = MARKET_DAILY_CSV) -> pd.DataFrame:
    return _normalize(pd.read_csv(filepath, index_col=0, parse_dates=True))


def _validate_recency(frame: pd.DataFrame, now: pd.Timestamp) -> None:
    """Accept the previous close pre-open, but not an old session during trading."""
    schedule = _NYSE.schedule(start_date=(now - pd.Timedelta(days=14)).date(), end_date=now.date())
    if schedule.empty:
        raise ValueError("Unable to establish the latest exchange session")
    # Allow the current session's daily feed its opening publication delay.
    eligible = schedule.loc[schedule.market_open <= now - pd.Timedelta(minutes=30)]
    if eligible.empty:
        raise ValueError("Unable to establish the latest exchange session")
    expected = pd.Timestamp(eligible.index[-1]).tz_localize(None).normalize()
    actual = frame["SPY"].dropna().index[-1]
    if actual < expected:
        raise ValueError(f"SPY daily data ends {actual.date()}; expected session {expected.date()}")
    if actual > now.tz_convert("America/New_York").tz_localize(None).normalize():
        raise ValueError("SPY daily data contains a future session")


def get_entry_history(allow_refresh: bool = True, filepath: Path | str = MARKET_DAILY_CSV,
                      now: pd.Timestamp | None = None) -> pd.DataFrame:
    path = Path(filepath)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    recently_updated = path.exists() and time.time() - path.stat().st_mtime < LIVE_CACHE_TTL_SEC
    cached_is_current = False
    if recently_updated:
        try:
            _validate_recency(entry_history(path), now)
            cached_is_current = True
        except (OSError, ValueError):
            pass
    attempted = not path.exists() or (allow_refresh and (FORCE_REFRESH.get() or not cached_is_current))
    refreshed = refresh_entry_history(path) if attempted else False
    frame = entry_history(path)
    _validate_recency(frame, now)
    frame.attrs["refresh_failed"] = bool(attempted and not refreshed)
    frame.attrs["retrieved_at"] = pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC").isoformat()
    return frame
