"""Daily corporate-credit spread context from FRED, retained across rolling source windows."""

from pathlib import Path
import logging

import numpy as np
import pandas as pd

from buysignal.collectors.cache import write_csv
from buysignal.collectors.fred import fetch_series_with_retries
from buysignal.config import CREDIT_CSV, CREDIT_SERIES

log = logging.getLogger(__name__)


def credit_history(filepath: Path | str = CREDIT_CSV) -> pd.DataFrame:
    history = pd.read_csv(filepath, parse_dates=["date"])
    if not {"date", "spread"}.issubset(history.columns) or history.empty:
        raise ValueError("Credit history is empty or malformed")
    history["spread"] = pd.to_numeric(history["spread"], errors="raise")
    if history.date.isna().any() or not np.isfinite(history.spread).all() or (history.spread < 0).any():
        raise ValueError("Credit spread observations must have dates and finite nonnegative values")
    return history.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def update_credit_data(filepath: Path | str = CREDIT_CSV) -> str | None:
    frame, error = fetch_series_with_retries(CREDIT_SERIES, "Corporate credit spread")
    if frame is None:
        return error or "Credit download failed"
    try:
        new = frame.rename(columns={"value": "spread"})[["date", "spread"]].copy()
        new["spread"] = pd.to_numeric(new["spread"], errors="raise")
        new["date"] = pd.to_datetime(new["date"], errors="raise")
        if new.empty or new.date.isna().any() or not np.isfinite(new.spread).all() or (new.spread < 0).any():
            raise ValueError("Invalid credit observations")
        today = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
        if new.date.max() > today:
            raise ValueError("Credit download contains a future observation")
        path = Path(filepath)
        try:
            old = credit_history(path)
        except (OSError, ValueError):
            old = pd.DataFrame(columns=["date", "spread"])
        if not old.empty and new.date.max() < old.date.max():
            raise ValueError("Credit download ends before the existing observation")
        merged = pd.concat([old, new]) if not old.empty else new
        merged = merged.drop_duplicates("date", keep="last").sort_values("date")
        write_csv(merged, path)
    except (ValueError, KeyError, TypeError) as exc:
        return f"Invalid credit snapshot: {exc}"
    return None
