"""Collect FINRA margin statistics (margin debt); cache to CSV.

finra.org sits behind Cloudflare Turnstile, which plain requests/headless Selenium
can't get past. A local Docker bypass service solves it and hands back clearance
cookies over HTTP, which get replayed through a plain `requests.Session` to fetch
the xlsx directly (no browser needed for the download itself). See config.CF_BYPASS_*;
runner.py starts the container on demand.
"""

from __future__ import annotations

import random
import time
from io import BytesIO
from pathlib import Path

import pandas as pd
import requests

from collectors.freshness import refreshed_today
from config import (
    CF_BYPASS_CONTAINER,
    CF_BYPASS_IMAGE,
    CF_BYPASS_PORT,
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

_COLUMN_MAP = {
    "Year-Month": "month",
    "Debit Balances in Customers' Securities Margin Accounts": "debit_balances",
    "Free Credit Balances in Customers' Cash Accounts": "free_credit_cash",
    "Free Credit Balances in Customers' Securities Margin Accounts": "free_credit_margin",
}


def _get_clearance() -> tuple[dict, str]:
    """Fetch Cloudflare clearance cookies + UA for MARGIN_STATS_URL from the local bypass service."""
    try:
        resp = requests.get(f"{CF_BYPASS_URL}/cookies", params={"url": MARGIN_STATS_URL}, timeout=60)
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(
            f"Cloudflare bypass service not reachable at {CF_BYPASS_URL}. Start it with: "
            f"docker run -d --name {CF_BYPASS_CONTAINER} -p {CF_BYPASS_PORT}:8000 {CF_BYPASS_IMAGE}"
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


def _fetch_with_retries() -> pd.DataFrame | None:
    """Fetch + parse the margin-statistics workbook with retry/backoff."""
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _parse_xlsx(_fetch_xlsx_bytes())
        except Exception as exc:
            if attempt + 1 == SCRAPE_RETRIES:
                print(
                    f"Margin debt fetch failed after {SCRAPE_RETRIES} tries "
                    f"({type(exc).__name__}: {exc}); cache unchanged."
                )
                return None

            delay = SCRAPE_BACKOFF_SEC * 2**attempt + random.uniform(0, 1)
            print(
                f"Margin debt attempt {attempt + 1} failed "
                f"({type(exc).__name__}); retry in {delay:.0f}s"
            )
            time.sleep(delay)

    return None


def update_margin_debt_data(filepath: Path | str = MARGIN_DEBT_CSV) -> None:
    """Refresh the cached FINRA margin-statistics snapshot."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    history = _fetch_with_retries()
    if history is None:
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(filepath, index=False)

    tier, message = data_freshness(history)
    latest = history.iloc[-1]
    print(
        f"Margin debt cache updated: {len(history)} months, latest "
        f"{latest['month'].date()} = {latest['debit_balances']:,.0f} ({tier}: {message})"
    )


def margin_history(filepath: Path | str = MARGIN_DEBT_CSV) -> pd.DataFrame:
    """Return monthly margin debt history, ascending by month."""
    df = pd.read_csv(filepath, parse_dates=["month"])
    return df.sort_values("month").reset_index(drop=True)


def data_freshness(
    history: pd.DataFrame,
    today: pd.Timestamp | None = None,
) -> tuple[str, str]:
    """Classify how current the latest cached month is, relative to `today`.

    FINRA publishes month M's data around the third week of month M+1, but that
    can slip toward month-end. "fresh" = within the normal one-month lag and not
    yet past the point a newer release would be expected; "refresh_due_soon" =
    normal lag, but late enough in the month that a newer release may already be
    out; "stale" = clearly overdue (2+ months behind once past the checkpoint, or
    3+ months behind regardless of day).
    """
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()
    latest = history["month"].iloc[-1]

    age_months = (today.year * 12 + today.month) - (latest.year * 12 + latest.month)
    past_checkpoint = today.day >= MARGIN_REFRESH_WINDOW_DAY
    label = latest.strftime("%Y-%m")

    if age_months <= 1:
        if past_checkpoint:
            return "refresh_due_soon", f"latest month {label} may be superseded any day; worth re-checking"
        return "fresh", f"latest month {label} is current"

    if age_months == 2 and not past_checkpoint:
        return "fresh", f"latest month {label} is current"

    return "stale", f"latest month {label} is {age_months} months old; FINRA release looks overdue, check the scraper"


def should_refresh(filepath: Path | str = MARGIN_DEBT_CSV, today: pd.Timestamp | None = None) -> bool:
    """Whether a refresh attempt is worth making right now.

    Margin debt only updates monthly, so skip almost the whole month (nothing new
    to find) and skip repeat attempts already made today once one has run.
    """
    filepath = Path(filepath)
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now()

    if not filepath.exists():
        return True

    if refreshed_today(filepath, today):
        return False

    tier, _ = data_freshness(margin_history(filepath), today)
    return tier != "fresh"


if __name__ == "__main__":
    update_margin_debt_data()
    print(margin_history().tail(6).to_string(index=False))
