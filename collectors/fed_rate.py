"""Collect CME FedWatch probabilities for three time horizons."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    FEDWATCH_CSV,
    FEDWATCH_URL,
    FETCH_JITTER_SEC,
    SCRAPE_BACKOFF_SEC,
    SCRAPE_RETRIES,
)

_HORIZONS = {
    "nearest": pd.DateOffset(),
    "six_month": pd.DateOffset(months=6),
    "one_year": pd.DateOffset(years=1),
}

_TABLE_XPATH = (
    "//table[contains(concat(' ', normalize-space(@class), ' '), ' grid-thm ')"
    " and .//th[normalize-space()='Ease']"
    " and .//th[normalize-space()='No Change']"
    " and .//th[normalize-space()='Hike']]"
)

_DATE_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")


def _build_driver() -> webdriver.Chrome:
    """Return a headless Chrome driver."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1200")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


def _percentage(text: str) -> float:
    """Convert a percentage string to a decimal probability."""
    return float(text.replace("%", "").strip()) / 100


def _fetch_meeting_probabilities() -> pd.DataFrame:
    """Scrape meeting probabilities into a DataFrame."""
    driver = _build_driver()

    try:
        driver.get(FEDWATCH_URL)
        wait = WebDriverWait(driver, 40)

        iframe = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "iframe[id^='cmeIframe-']")
            )
        )
        driver.switch_to.frame(iframe)

        probability_tab = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[normalize-space()='Probabilities']")
            )
        )
        driver.execute_script("arguments[0].click()", probability_tab)

        tables = wait.until(
            EC.presence_of_all_elements_located((By.XPATH, _TABLE_XPATH))
        )

        rows: list[dict] = []
        seen_dates: set[pd.Timestamp] = set()

        for table in tables:
            try:
                card = table.find_element(
                    By.XPATH,
                    "ancestor::*[contains(normalize-space(.), '/')][1]",
                )
                date_match = _DATE_PATTERN.search(card.text)
                values = table.find_elements(
                    By.CSS_SELECTOR,
                    "tr:last-child td.number",
                )

                if not date_match or len(values) != 3:
                    continue

                meeting_date = pd.to_datetime(
                    date_match.group(),
                    format="%m/%d/%Y",
                )

                if meeting_date in seen_dates:
                    continue

                ease, no_change, hike = map(
                    _percentage,
                    (cell.text for cell in values),
                )

                rows.append(
                    {
                        "meeting_date": meeting_date,
                        "prob_ease": ease,
                        "prob_no_change": no_change,
                        "prob_hike": hike,
                    }
                )
                seen_dates.add(meeting_date)

            except (ValueError, TypeError):
                continue

        if not rows:
            raise ValueError("No FedWatch meeting probabilities found.")

        return (
            pd.DataFrame(rows)
            .sort_values("meeting_date")
            .reset_index(drop=True)
        )

    finally:
        driver.quit()


def _select_meetings(
    meetings: pd.DataFrame,
    today: pd.Timestamp,
) -> pd.DataFrame:
    """Select the nearest, six-month, and one-year meetings."""
    rows = []

    for horizon, offset in _HORIZONS.items():
        target = today + offset
        distance = (meetings["meeting_date"] - target).abs()
        candidates = meetings.loc[distance == distance.min()]
        meeting = candidates.sort_values("meeting_date").iloc[-1]

        rows.append({**meeting.to_dict(), "horizon": horizon})

    return pd.DataFrame(rows)


def _fetch_with_retries() -> pd.DataFrame | None:
    """Fetch meeting probabilities with retry/backoff."""
    for attempt in range(SCRAPE_RETRIES):
        try:
            return _fetch_meeting_probabilities()
        except Exception as exc:
            if attempt + 1 == SCRAPE_RETRIES:
                print(
                    f"FedWatch scrape failed after {SCRAPE_RETRIES} tries "
                    f"({type(exc).__name__}); cache unchanged."
                )
                return None

            delay = (
                SCRAPE_BACKOFF_SEC * 2**attempt
                + random.uniform(0, 1)
            )
            print(
                f"FedWatch attempt {attempt + 1} failed "
                f"({type(exc).__name__}); retry in {delay:.0f}s"
            )
            time.sleep(delay)

    return None


def update_fed_rate_data(filepath: Path | str = FEDWATCH_CSV) -> None:
    """Refresh the cached FedWatch snapshot."""
    filepath = Path(filepath)
    time.sleep(random.uniform(*FETCH_JITTER_SEC))

    meetings = _fetch_with_retries()
    if meetings is None:
        return

    snapshot = _select_meetings(
        meetings,
        pd.Timestamp.now().normalize(),
    )

    filepath.parent.mkdir(parents=True, exist_ok=True)
    snapshot.to_csv(filepath, index=False)

    print(
        f"FedWatch cache updated: {len(snapshot)} meetings, "
        f"{snapshot['meeting_date'].min().date()} -> "
        f"{snapshot['meeting_date'].max().date()}"
    )


def latest_fedwatch(
    filepath: Path | str = FEDWATCH_CSV,
) -> pd.DataFrame:
    """Load the cached FedWatch snapshot."""
    return pd.read_csv(filepath, parse_dates=["meeting_date"])


if __name__ == "__main__":
    update_fed_rate_data()
    print(latest_fedwatch().to_string(index=False))
