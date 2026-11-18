"""Collect FINRA monthly balances from its official workbook."""

from __future__ import annotations

import logging
import math
import random
import time
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

from buysignal.collectors.cache import write_csv

from buysignal.config import (
    FETCH_JITTER_SEC,
    MARGIN_DEBT_CSV,
    MARGIN_REFRESH_WINDOW_DAY,
    MARGIN_SHEET_NAME,
    MARGIN_XLSX_URL,
    SCRAPE_BACKOFF_SEC,
    SCRAPE_RETRIES,
)

log = logging.getLogger(__name__)

_COLUMN_MAP = {
    "Year-Month": "month",
    "Debit Balances in Customers' Securities Margin Accounts": "debit_balances",
    "Free Credit Balances in Customers' Cash Accounts": "free_credit_cash",
    "Free Credit Balances in Customers' Securities Margin Accounts": "free_credit_margin",
}


def _fetch_xlsx_bytes() -> bytes:
    """FINRA's official workbook (an xlsx is a zip, so it starts with "PK")."""
    response = requests.get(MARGIN_XLSX_URL, timeout=30, headers={"User-Agent": "BuySignal/1.0"})
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise ValueError("FINRA returned non-workbook content")
    return response.content


def _parse_xlsx(raw: bytes) -> pd.DataFrame:
    """Parse the FINRA workbook into a tidy, ascending-by-month DataFrame."""
    df = pd.read_excel(BytesIO(raw), sheet_name=MARGIN_SHEET_NAME)
    df = df.rename(columns=_COLUMN_MAP)
    df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
    return df.sort_values("month").reset_index(drop=True)


def _fetch_with_retries() -> tuple[pd.DataFrame | None, str | None]:
    """Fetch + parse the margin-statistics workbook with retry/backoff. Returns (data, error_message)."""
    last_error = None
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _parse_xlsx(_fetch_xlsx_bytes()), None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 == SCRAPE_RETRIES:
                log.error("Margin debt fetch failed after %d tries (%s); cache unchanged.", SCRAPE_RETRIES, last_error)
                return None, last_error

            delay = SCRAPE_BACKOFF_SEC * 2**attempt + random.uniform(0, 1)
            log.debug("Margin debt attempt %d failed (%s); retry in %.0fs", attempt + 1, last_error, delay)
            time.sleep(delay)

    return None, last_error


def update_margin_debt_data(filepath: Path | str = MARGIN_DEBT_CSV) -> str | None:
    """Refresh the cached FINRA margin-statistics snapshot. Returns an error message, or None on success."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    history, error = _fetch_with_retries()
    if history is None:
        return error or "unknown error"

    filepath.parent.mkdir(parents=True, exist_ok=True)
    if history.empty or history.month.isna().any() or history.month.duplicated().any():
        return "Invalid FINRA month sequence; cache unchanged"
    balances = pd.to_numeric(history.debit_balances, errors="coerce")
    if balances.isna().any() or not balances.map(math.isfinite).all() or (balances <= 0).any():
        return "Invalid FINRA debit balances; cache unchanged"
    month_numbers = history.month.dt.year * 12 + history.month.dt.month
    if not month_numbers.diff().dropna().eq(1).all():
        return "FINRA month sequence has gaps; cache unchanged"
    if history.month.max() > pd.Timestamp.now().normalize().replace(day=1):
        return "FINRA snapshot contains a future month; cache unchanged"
    if filepath.exists():
        try:
            if history.month.max() < margin_history(filepath).month.max():
                return "FINRA snapshot moved backwards; cache unchanged"
        except (OSError, ValueError, KeyError):
            pass
    write_csv(history, filepath)

    tier, message = data_freshness(history)
    latest = history.iloc[-1]
    log.info(
        "Margin debt cache updated: %d months, latest %s = %s (%s: %s)",
        len(history),
        latest["month"].date(),
        f"{latest['debit_balances']:,.0f}",
        tier,
        message,
    )
    return None


def margin_history(filepath: Path | str = MARGIN_DEBT_CSV) -> pd.DataFrame:
    """Return monthly margin debt history, ascending by month."""
    df = pd.read_csv(filepath, parse_dates=["month"])
    return df.sort_values("month").reset_index(drop=True)


def data_freshness(
    history: pd.DataFrame,
    today: pd.Timestamp | None = None,
) -> tuple[str, str]:
    """Classify the latest cached month against `today`: fresh, refresh_due_soon or stale.

    FINRA publishes month M around the third week of M+1. fresh: at most one month behind, or two
    before that checkpoint. refresh_due_soon: two behind after it. stale: three or more, which points
    to a broken scraper.
    """
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    latest = history["month"].iloc[-1]

    age_months = (today.year * 12 + today.month) - (latest.year * 12 + latest.month)
    past_checkpoint = today.day >= MARGIN_REFRESH_WINDOW_DAY
    label = latest.strftime("%Y-%m")

    if age_months <= 1:
        return "fresh", f"latest month {label} is current"

    if age_months == 2:
        if past_checkpoint:
            return "refresh_due_soon", f"latest month {label} is 2 months old and the release checkpoint has passed; worth re-checking"
        return "fresh", f"latest month {label} is current"

    return "stale", f"latest month {label} is {age_months} months old; FINRA release looks overdue, check the scraper"


def demands_refresh(filepath: Path | str = MARGIN_DEBT_CSV, today: pd.Timestamp | None = None) -> bool:
    """Refresh every tick once FINRA's checkpoint passes with the release still missing; otherwise daily."""
    filepath = Path(filepath)
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()

    if not filepath.exists():
        return True

    try:
        history = margin_history(filepath)
    except Exception:
        return True  # truncated by a kill mid-write: refetch repairs it, and never raise here

    tier, _ = data_freshness(history, today)
    # A stale cache points to a broken scraper; hourly retries would not fix that.
    return tier == "refresh_due_soon"


if __name__ == "__main__":
    from buysignal import logs

    logs.configure()
    update_margin_debt_data()
    print(margin_history().tail(6).to_string(index=False))
