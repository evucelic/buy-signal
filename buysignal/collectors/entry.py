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
import threading

import numpy as np
import pandas as pd
import yfinance as yf

from buysignal.collectors.cache import write_csv
from buysignal.config import INDEX_TICKERS, OPTIONAL_INDEX_TICKERS, MARKET_DAILY_CSV, MARKET_DAILY_PERIOD, MARKET_BOOTSTRAP_PERIOD, FETCH_JITTER_SEC, LIVE_CACHE_TTL_SEC, YAHOO_TIMEOUT_SEC
from buysignal.resources import FORCE_REFRESH
from buysignal.schedule.market_hours import _NYSE

log = logging.getLogger(__name__)
_REFRESH_LOCK = threading.RLock()
_RETRY_AFTER_SEC = 60


def _utc_now():
    return pd.Timestamp.now(tz="UTC")


def _normalize(frame: pd.DataFrame, *, require_spy: bool = True) -> pd.DataFrame:
    frame = frame.copy()
    index = pd.to_datetime(frame.index)
    if index.tz is not None:
        index = index.tz_convert("America/New_York").tz_localize(None)
    frame.index = index.normalize()
    frame.index.name = "date"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if frame.empty:
        raise ValueError("Daily index history is empty")
    for name in INDEX_TICKERS:
        if name not in frame:
            frame[name] = np.nan
    if frame.index.isna().any() or (require_spy and frame["SPY"].dropna().empty):
        raise ValueError("Daily history is missing SPY observations or dates")
    for name in INDEX_TICKERS:
        if name != 'SPY':
            numeric = pd.to_numeric(frame[name], errors='coerce')
            frame[name] = numeric.where(np.isfinite(numeric) & (numeric > 0))
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


def _merge_series(old: pd.Series, new: pd.Series) -> pd.Series:
    """Reconcile one ticker; a missing peer must never erase its observations."""
    old, new = old.dropna(), new.dropna()
    if new.empty:
        return old
    if old.empty:
        return new
    common = old.index.intersection(new.index)
    common = common[common < max(old.index[-1], new.index[-1])]
    if len(common) < 2:
        raise ValueError("Insufficient completed overlap for adjusted-price reconciliation")
    ratios = new.loc[common[:2]] / old.loc[common[:2]]
    if not np.allclose(ratios, ratios.iloc[0], rtol=1e-5, atol=1e-8):
        raise ValueError("Nonuniform price revision; full reconciliation required")
    result = old * float(ratios.iloc[0])
    result = result.reindex(result.index.union(new.index))
    result.loc[new.index] = new
    return result


def _download(period: str, names=None) -> pd.DataFrame:
    mapping = INDEX_TICKERS | OPTIONAL_INDEX_TICKERS
    names = list(mapping) if names is None else list(names)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))
    raw = yf.download([mapping[name] for name in names], period=period, interval="1d", auto_adjust=True,
                      progress=False, threads=False, timeout=YAHOO_TIMEOUT_SEC)
    if raw is None or raw.empty or "Close" not in raw:
        raise ValueError("Daily index download returned no data")
    close = raw["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(name=mapping[names[0]])
    return _normalize(close.rename(columns={v: k for k, v in mapping.items()}), require_spy=False)


def _metadata(path):
    try:
        return json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_metadata(path, metadata):
    target = path.with_suffix(".meta.json")
    temporary = target.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(metadata))
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)


def refresh_entry_history(filepath: Path | str = MARKET_DAILY_CSV) -> bool:
    with _REFRESH_LOCK:
        return _refresh_entry_history(Path(filepath))


def _refresh_entry_history(path: Path) -> bool:
    """One batch plus small, targeted repairs. Retain each ticker independently."""
    now = _utc_now()
    today = now.tz_convert("America/New_York").date().isoformat()
    metadata = _metadata(path)
    metadata["last_attempt"] = now.isoformat()
    try:
        old = entry_history(path)
    except (OSError, ValueError):
        old = None
    bootstrap = old is None or metadata.get("bootstrap_scope") != MARKET_BOOTSTRAP_PERIOD
    full = (bootstrap or FORCE_REFRESH.get() or metadata.get("full_refresh_date") != today
            or metadata.get("optional_indexes") != list(OPTIONAL_INDEX_TICKERS))
    period = MARKET_BOOTSTRAP_PERIOD if bootstrap else MARKET_DAILY_PERIOD if full else "5d"
    errors = {}
    try:
        new = _download(period)
    except Exception as exc:
        new = pd.DataFrame()
        errors["batch"] = f"{type(exc).__name__}: {exc}"
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    series = {}
    expected = _expected_session(now)
    for name in (*INDEX_TICKERS, *OPTIONAL_INDEX_TICKERS):
        previous = old[name].dropna() if old is not None and name in old else empty
        fresh = new[name].dropna() if name in new else empty
        # Only SPY is an entry gate. Peer outages are repaired independently.
        needs_repair = name in INDEX_TICKERS and (fresh.empty or fresh.index[-1] < expected)
        if needs_repair:
            try:
                repair = _download("5d" if not previous.empty else period, [name])[name].dropna()
                if not repair.empty:
                    fresh = repair.combine_first(fresh).sort_index()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
        try:
            merged = _merge_series(previous, fresh)
        except ValueError:
            # Corporate actions/revisions need one ticker's complete history,
            # not another maximum-history download for all four indexes.
            try:
                merged = _download(MARKET_BOOTSTRAP_PERIOD, [name])[name].dropna()
                if merged.empty or (not previous.empty and merged.index[-1] < previous.index[-1]):
                    raise ValueError("Reconciliation returned older or empty history")
            except Exception as exc:
                merged = previous
                errors[name] = f"{type(exc).__name__}: {exc}"
        if not merged.empty:
            series[name] = merged
    try:
        frame = _normalize(pd.DataFrame(series))
        if len(frame.SPY.dropna()) < 64:
            raise ValueError("Daily history contains fewer than 64 SPY sessions")
        # Never publish future observations, or mistake a stale response for a refresh.
        local_today = now.tz_convert("America/New_York").tz_localize(None).normalize()
        if any(values.index[-1] > local_today for values in series.values()):
            raise ValueError("Daily history contains a future session")
        current = frame.SPY.dropna().index[-1] >= expected
        if current and not new.empty:
            # A peer may be lagging; its own as-of date remains intact in the union.
            write_csv(frame, path, index=True)
            metadata["last_success"] = now.isoformat()

        elif old is None:
            # Useful historical data can bootstrap an offline view, but is not current.
            write_csv(frame, path, index=True)
        elif not frame.equals(old):
            write_csv(frame, path, index=True)
            metadata["last_success"] = now.isoformat()
        if full and not new.empty:
            metadata.update(full_refresh_date=today, bootstrap_scope=MARKET_BOOTSTRAP_PERIOD,
                            optional_indexes=list(OPTIONAL_INDEX_TICKERS))
        metadata["errors"] = errors
        metadata["observed"] = {k: str(v.index[-1].date()) for k, v in series.items()}
        metadata["stale"] = not current
        if not current:
            metadata["errors"]["SPY"] = f"Expected {expected.date()}; latest {frame.SPY.dropna().index[-1].date()}"
        _write_metadata(path, metadata)
        if not current:
            log.warning("Price source is delayed; retaining dated observations: %s", metadata["errors"])
        return bool(current or not new.empty)
    except Exception as exc:
        metadata["errors"] = {**errors, "SPY": f"{type(exc).__name__}: {exc}"}
        _write_metadata(path, metadata)
        log.warning("Daily index refresh failed (%s: %s); cache retained", type(exc).__name__, exc)
        return False


def entry_history(filepath: Path | str = MARKET_DAILY_CSV) -> pd.DataFrame:
    return _normalize(pd.read_csv(filepath, index_col=0, parse_dates=True))


def _expected_session(now: pd.Timestamp) -> pd.Timestamp:
    schedule = _NYSE.schedule(start_date=(now - pd.Timedelta(days=14)).date(), end_date=now.date())
    eligible = schedule.loc[schedule.market_open <= now - pd.Timedelta(minutes=30)]
    if eligible.empty:
        raise ValueError("Unable to establish the latest exchange session")
    return pd.Timestamp(eligible.index[-1]).tz_localize(None).normalize()


def _validate_recency(frame: pd.DataFrame, now: pd.Timestamp) -> None:
    expected = _expected_session(now)
    actual = frame.SPY.dropna().index[-1]
    if actual < expected:
        raise ValueError(f"SPY daily data ends {actual.date()}; expected session {expected.date()}")
    if actual > now.tz_convert("America/New_York").tz_localize(None).normalize():
        raise ValueError("SPY daily data contains a future session")


def get_entry_history(allow_refresh: bool = True, filepath: Path | str = MARKET_DAILY_CSV,
                      now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Serve last-known prices through outages; freshness is a separate explicit fact."""
    path = Path(filepath)
    now = _utc_now() if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    with _REFRESH_LOCK:
        metadata = _metadata(path)
        recent = path.exists() and time.time() - path.stat().st_mtime < LIVE_CACHE_TTL_SEC
        try:
            frame = entry_history(path)
            _validate_recency(frame, now)
            current = True
        except (OSError, ValueError):
            current = False
        attempted_at = metadata.get("last_attempt")
        try:
            age = (now - pd.Timestamp(attempted_at)).total_seconds() if attempted_at else None
            cooling_down = age is not None and 0 <= age < _RETRY_AFTER_SEC
        except (ValueError, TypeError):
            cooling_down = False
        attempted = (not path.exists() or (allow_refresh and (FORCE_REFRESH.get() or
                     (not cooling_down and (not recent or not current)))))
        refreshed = refresh_entry_history(path) if attempted else False
        frame = entry_history(path)
        # Corrupt/future prices are errors, not an availability fallback.
        actual = frame.SPY.dropna().index[-1]
        if actual > now.tz_convert("America/New_York").tz_localize(None).normalize():
            raise ValueError("SPY daily data contains a future session")
        expected = _expected_session(now)
        metadata = _metadata(path)
        frame.attrs.update(
            refresh_failed=bool((attempted and not refreshed) or actual < expected),
            stale=bool(actual < expected), expected_session=str(expected.date()),
            retrieved_at=metadata.get("last_success") or pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC").isoformat(),
            source_errors=metadata.get("errors", {}),
        )
        return frame
