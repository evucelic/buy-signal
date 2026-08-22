"""Collect segment valuation snapshots (trailing + forward P/E) from MSCI; accumulate in CSV.

Both ratios come from MSCI index pages — one provider, one methodology, and "P/E Fwd" is a
true next-year consensus forward P/E (verified against the labeled values on msci.com). The
site sits behind a JS challenge that blocks plain requests, so pages are rendered with
headless Selenium (same infrastructure fed_rate.py already uses daily in production). MSCI
refreshes the values monthly; one row per segment per day is APPENDED to the cache with the
page's as-of date, and the accumulated unique-as-of history is what makes z-scores ("cheap
vs own history") possible. Never overwrite this file wholesale.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from collectors.freshness import refreshed_today
from config import (
    FETCH_JITTER_SEC,
    MSCI_INDEX_URL,
    MSCI_PAGE_LOAD_TIMEOUT_SEC,
    MSCI_RENDER_WAIT_SEC,
    OPPORTUNITY_SEGMENTS,
    USER_AGENTS,
    VALUATIONS_CSV,
)

_METRICS = re.compile(r"P/E \| ([\d.,]+) \| P/E Fwd \| ([\d.,]+)")
_ASOF = re.compile(r"as of (\w+ \d{1,2}, \d{4})", re.IGNORECASE)


def _build_driver() -> webdriver.Chrome:
    """Return a headless Chrome driver (mirrors fed_rate's setup)."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1200")
    options.add_argument(f"user-agent={random.choice(USER_AGENTS)}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(MSCI_PAGE_LOAD_TIMEOUT_SEC)
    return driver


def _parse_metrics(body_text: str) -> tuple[float, float, str | None]:
    """Extract (trailing P/E, forward P/E, as-of date) from a rendered MSCI index page."""
    flat = " | ".join(line.strip() for line in body_text.splitlines() if line.strip())
    match = _METRICS.search(flat)
    if not match:
        raise ValueError("P/E metrics not found on page (challenge not solved or layout changed)")
    asof = _ASOF.search(flat)
    trailing = float(match.group(1).replace(",", ""))
    fwd = float(match.group(2).replace(",", ""))
    return trailing, fwd, asof.group(1) if asof else None


def _fetch_index_metrics(driver: webdriver.Chrome, msci_code: str) -> tuple[float, float, str | None]:
    """Render one MSCI index page and parse its valuation metrics."""
    driver.get(MSCI_INDEX_URL.format(code=msci_code))
    time.sleep(MSCI_RENDER_WAIT_SEC)  # JS challenge + SPA render
    return _parse_metrics(driver.find_element(By.TAG_NAME, "body").text)


def update_valuations_data(filepath: Path | str = VALUATIONS_CSV) -> str | None:
    """Append today's per-segment valuation snapshot. Returns an error message, or None.

    Per-segment failures are collected rather than fatal: whatever rendered successfully
    is still written, so one broken page doesn't stall the history.
    """
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    try:
        driver = _build_driver()
    except Exception as exc:
        return f"webdriver: {type(exc).__name__}: {exc}"

    today = pd.Timestamp.now().normalize()
    rows, errors = [], []
    try:
        for segment, spec in OPPORTUNITY_SEGMENTS.items():
            try:
                trailing, fwd, asof = _fetch_index_metrics(driver, spec["msci_code"])
                rows.append(
                    {"date": today, "segment": segment, "fwd_pe": fwd, "trailing_pe": trailing, "asof": asof}
                )
            except Exception as exc:
                errors.append(f"{segment}: {type(exc).__name__}: {exc}")
    finally:
        driver.quit()

    if rows:
        new = pd.DataFrame(rows)
        if filepath.exists():
            old = pd.read_csv(filepath, parse_dates=["date"])
            new = pd.concat([old, new])
        new = new.drop_duplicates(subset=["date", "segment"], keep="last").sort_values(["date", "segment"])
        filepath.parent.mkdir(parents=True, exist_ok=True)
        new.to_csv(filepath, index=False)
        print(f"Valuations cache updated: {len(rows)} segments for {today.date()} ({len(new)} rows total).")

    return "; ".join(errors) if errors else None


def valuations_history(filepath: Path | str = VALUATIONS_CSV) -> pd.DataFrame:
    """Return the accumulated valuation snapshots, ascending by (date, segment)."""
    df = pd.read_csv(filepath, parse_dates=["date"])
    return df.sort_values(["date", "segment"]).reset_index(drop=True)


def should_refresh(filepath: Path | str = VALUATIONS_CSV) -> bool:
    """One snapshot per day; MSCI only changes the values monthly anyway."""
    return not refreshed_today(filepath)


if __name__ == "__main__":
    update_valuations_data()
    print(valuations_history().tail(8).to_string(index=False))
