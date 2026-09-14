"""Collect FINRA margin statistics (margin debt); cache to CSV.

finra.org sits behind Cloudflare Turnstile, which plain requests/headless Selenium
can't get past. The cfbypass compose service solves it and hands back clearance
cookies over HTTP, which get replayed through a plain `requests.Session` to fetch
the xlsx directly (no browser needed for the download itself). See config.CF_BYPASS_*.
"""

from __future__ import annotations

import logging
import random
import time
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

from buysignal.config import (
    CF_BYPASS_TIMEOUT_SEC,
    CF_BYPASS_URL,
    FETCH_JITTER_SEC,
    MARGIN_DEBT_CSV,
    MARGIN_REFRESH_WINDOW_DAY,
    MARGIN_SHEET_NAME,
    MARGIN_STATS_URL,
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


def _get_clearance() -> tuple[dict, str]:
    """Fetch Cloudflare clearance cookies + UA for MARGIN_STATS_URL from the local bypass service."""
    try:
        resp = requests.get(f"{CF_BYPASS_URL}/cookies", params={"url": MARGIN_STATS_URL}, timeout=CF_BYPASS_TIMEOUT_SEC)
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Cloudflare bypass service not reachable at {CF_BYPASS_URL}. "
            "Start it with: docker compose up -d cfbypass"
        ) from exc
    resp.raise_for_status()
    data = resp.json()
    return data["cookies"], data["user_agent"]


def _fetch_xlsx_bytes() -> bytes:
    """Download the margin-statistics workbook using a bypassed Cloudflare session."""
    cookies, user_agent = _get_clearance()

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".finra.org")

    resp = session.get(MARGIN_XLSX_URL, timeout=60)
    resp.raise_for_status()
    return resp.content


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
    history.to_csv(filepath, index=False)

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
