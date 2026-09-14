"""Shared FRED series fetch for the collectors that read from fredgraph.csv.

FRED's fredgraph.csv endpoint serves the full daily history of a series without an API
key. Missing observations (market holidays etc.) come through as blank values (NaN after
read_csv), so they're dropped.
"""

from __future__ import annotations

import logging
import random
import time
from io import StringIO

import pandas as pd
import requests

from buysignal.config import FRED_GRAPH_CSV_URL, SCRAPE_BACKOFF_SEC, SCRAPE_RETRIES

log = logging.getLogger(__name__)


def fetch_series(series_id: str) -> pd.DataFrame:
    """Download a FRED series' full daily history as columns (date, value), ascending."""
    resp = requests.get(FRED_GRAPH_CSV_URL, params={"id": series_id}, timeout=60)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), parse_dates=["observation_date"])
    df = df.rename(columns={"observation_date": "date", series_id: "value"})
    return df.dropna(subset=["value"]).sort_values("date").reset_index(drop=True)


def fetch_series_with_retries(series_id: str, label: str) -> tuple[pd.DataFrame | None, str | None]:
    """fetch_series with retry/backoff. Returns (data, error_message); label is for logging."""
    last_error = None
    for attempt in range(SCRAPE_RETRIES):
        try:
            return fetch_series(series_id), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 == SCRAPE_RETRIES:
                log.error("%s fetch failed after %d tries (%s); cache unchanged.", label, SCRAPE_RETRIES, last_error)
                return None, last_error

            delay = SCRAPE_BACKOFF_SEC * 2**attempt + random.uniform(0, 1)
            log.debug("%s attempt %d failed (%s); retry in %.0fs", label, attempt + 1, last_error, delay)
            time.sleep(delay)

    return None, last_error
