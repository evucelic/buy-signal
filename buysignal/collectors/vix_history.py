"""Daily VIX closes from FRED (VIXCLS, from 1990), for context only.

The hourly Yahoo cache drives the fear rule but covers about two years; this says how unusual today is.
"""

from pathlib import Path
import logging

import numpy as np
import pandas as pd

from buysignal.collectors.cache import write_csv
from buysignal.collectors.fred import fetch_series_with_retries
from buysignal.config import VIX_HISTORY_CSV, VIX_HISTORY_SERIES

log = logging.getLogger(__name__)
_MAX_PLAUSIBLE = 200.0  # the record close is under 90; anything near this is a broken download


def _validate(frame: pd.DataFrame) -> pd.DataFrame:
    if not {"date", "vix"}.issubset(frame.columns) or frame.empty:
        raise ValueError("VIX history is empty or malformed")
    frame = frame[["date", "vix"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    frame["vix"] = pd.to_numeric(frame["vix"], errors="raise")
    values = frame.vix.to_numpy(dtype=float)
    if frame.date.isna().any() or not np.isfinite(values).all() or (values <= 0).any() or (values > _MAX_PLAUSIBLE).any():
        raise ValueError("VIX history needs dates and positive, plausible closes")
    return frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def vix_long_history(filepath: Path | str = VIX_HISTORY_CSV) -> pd.DataFrame:
    """Daily closes (date, vix), ascending. Raises when the file is absent or invalid."""
    return _validate(pd.read_csv(filepath, parse_dates=["date"]))


def update_vix_history(filepath: Path | str = VIX_HISTORY_CSV) -> str | None:
    frame, error = fetch_series_with_retries(VIX_HISTORY_SERIES, "VIX daily history")
    if frame is None:
        return error or "VIX history download failed"
    try:
        new = _validate(frame.rename(columns={"value": "vix"}))
        today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
        if new.date.max() > today:
            raise ValueError("download contains a future observation")
        path = Path(filepath)
        try:
            old = vix_long_history(path)
        except (OSError, ValueError):
            old = None
        if old is not None and new.date.max() < old.date.max():
            raise ValueError("download ends before the existing observation")
        merged = new if old is None else pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
        write_csv(merged, path)
    except (ValueError, KeyError, TypeError) as exc:
        return f"Invalid VIX history snapshot: {exc}"
    return None


def with_long_history(recent: pd.DataFrame, filepath: Path | str = VIX_HISTORY_CSV) -> pd.DataFrame:
    """Prepend older daily closes to the hourly cache's daily rows; the recent rows always win."""
    try:
        older = vix_long_history(filepath)
    except (OSError, ValueError, KeyError):
        return recent
    older = older[older.date < recent.date.min()]
    if older.empty:
        return recent
    combined = pd.concat([older, recent], ignore_index=True)
    combined.attrs.update(recent.attrs)
    return combined
